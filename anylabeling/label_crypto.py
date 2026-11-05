"""AES-256-GCM envelopes and role-separated keys for label files.

``.xalenc`` payload (unchanged ``XAL1`` layout)
-----------------------------------------------

    offset 0  : 4 bytes  magic ``b"XAL1"``
    offset 4  : 1 byte   version (currently ``1``)
    offset 5  : 16 bytes key id, ``SHA-256(DEK)[:16]``
    offset 21 : 12 bytes random nonce
    offset 33 : AES-256-GCM ciphertext with the 16-byte tag appended

The GCM associated data is ``magic || version || key_id``. ``key_id`` is
always the data key (DEK), never the master key. Existing ``.xalenc`` files
stay valid: treat the old ``project.key`` as the DEK and wrap it.

Roles
-----

* **Algorithm** holds ``master.key`` (32 raw bytes, same file format as
  ``xlabel-keygen``) and the sibling ``<master.key>.xalstore``. Only that
  master can run ``xlabel-decrypt`` / ``xlabel-encrypt``. A ``.xalk`` file
  or a passphrase is rejected on those commands.
* **Annotator** holds a ``.xalk`` credential. The GUI unwraps the DEK with
  a passphrase into process memory and then uses the existing XAL1
  encrypt/decrypt path. The raw DEK and the master key are not in the
  credential. Annotator sessions cannot export YOLO/COCO/VOC/DOTA/mask/
  PPOCR/MOT (and the other plaintext training formats).

``.xalk`` contains two wraps of the same DEK: one under scrypt(passphrase)
and one under the master key. The keystore holds the master wrap again so
batch decrypt needs only ``master.key`` on the algorithm machine.

Upgrade (no re-annotation)
--------------------------

1. ``xlabel-keygen -o master.key`` on the algorithm machine.
2. ``xlabel-wrap --master master.key --dek project.key -o annotator.xalk``
   using the key that already matches the ``.xalenc`` files.
3. Annotators set ``label_credential_file`` to ``annotator.xalk`` and enter
   the passphrase. Do not put ``master.key`` or ``project.key`` in that
   setting.
4. The algorithm machine sets ``label_master_key_file`` to ``master.key``.
   Leave that empty on annotator machines.
5. Delete ``project.key`` from annotator machines. The ciphertext is not
   rewritten.

Threat model: copying ``.xalenc`` without the master does not yield JSON,
and an annotator credential cannot drive ``xlabel-decrypt``. This does not
stop memory dumps, screenshots, or a malicious local plugin. Plaintext
training export is equivalent to decryption, so the annotator role refuses
it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import os.path as osp
import secrets
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MAGIC = b"XAL1"
VERSION = 1
KEY_LEN = 32
KEY_ID_LEN = 16
NONCE_LEN = 12
TAG_LEN = 16
HEADER_LEN = 4 + 1 + KEY_ID_LEN + NONCE_LEN
ENCRYPTED_SUFFIX = ".xalenc"
METADATA_SUFFIX = ".meta.json"
CREDENTIAL_SUFFIX = ".xalk"
CREDENTIAL_FORMAT = "XALK1"
CREDENTIAL_VERSION = 1
KEYSTORE_SUFFIX = ".xalstore"
KEYSTORE_FORMAT = "XALSTORE1"
ROLE_ANNOTATOR = "annotator"
ROLE_ALGORITHM = "algorithm"
MIN_PASSPHRASE_LEN = 8
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1

MISSING_KEY_MESSAGE = (
    "Label encryption key is not unlocked. Annotators set "
    "label_credential_file to a .xalk credential; the algorithm machine "
    "sets label_master_key_file to the master key. Refusing to write "
    "plaintext."
)
LEGACY_KEY_MESSAGE = (
    "label_key_file is a raw key and is no longer loaded. Annotators "
    "cannot hold the master key or the data key. On the algorithm machine "
    "run: xlabel-keygen -o master.key && xlabel-wrap --master master.key "
    "--dek <old label_key_file> -o annotator.xalk. Existing .xalenc files "
    "stay valid (the old key is the DEK and is not rewritten). Then set "
    "label_credential_file on annotator machines and label_master_key_file "
    "on the algorithm machine."
)
PASSPHRASE_REQUIRED_MESSAGE = (
    "Annotator passphrase is required to unlock the .xalk credential. "
    "Enter it when prompted or set XLABEL_PASSPHRASE. xlabel-decrypt does "
    "not accept this passphrase."
)
RAW_KEY_AS_CREDENTIAL = (
    "Refusing a raw 32-byte key as the annotator credential. "
    "label_credential_file must be a .xalk file from xlabel-wrap. "
    "Put the master key only in label_master_key_file on the algorithm "
    "machine."
)
DECRYPT_REJECTS_CREDENTIAL = (
    "xlabel-decrypt accepts only a master key, not an annotator .xalk "
    "credential or a passphrase wrap."
)
NO_WRAPPED_DEK_MESSAGE = (
    "master key has no wrapped data key. Run xlabel-wrap --master "
    "<master.key> --dek <existing project.key> -o annotator.xalk. "
    "Existing .xalenc files stay valid; the old project key is the DEK. "
    "xlabel-decrypt does not accept a raw DEK, a .xalk file, or a "
    "passphrase."
)
TRAINING_EXPORT_DENIED = (
    "Plaintext training export is disabled for the annotator credential. "
    "YOLO, COCO, VOC, DOTA, mask, PPOCR, and MOT exports run only on the "
    "algorithm machine after the master key unlocks the data key."
)


class LabelCryptoError(Exception):
    """Encryption or decryption failed closed."""


class NoWrappedDekError(LabelCryptoError):
    """Master file loaded, but no DEK is wrapped for it."""


class TrainingExportDenied(LabelCryptoError):
    """Annotator role tried to write a plaintext training format."""


@dataclass(frozen=True)
class KeyMaterial:
    key: bytes
    key_id: bytes
    path: str | None = None

    def __repr__(self) -> str:
        return f"KeyMaterial(key_id={self.key_id.hex()}, path={self.path!r})"


@dataclass(frozen=True)
class EnvelopeParts:
    version: int
    key_id: bytes
    nonce: bytes
    body: bytes


@dataclass
class _KeyCache:
    path: str | None = None
    credential_path: str | None = None
    master_path: str | None = None
    passphrase: str | None = None
    material: KeyMaterial | None = None
    active_id: bytes | None = None
    deks: dict = field(default_factory=dict)
    role: str | None = None
    error: str | None = None
    loaded: bool = False

    def __repr__(self) -> str:
        return (
            "_KeyCache("
            f"path={self.path!r}, role={self.role!r}, "
            f"loaded={self.loaded}, error={self.error!r})"
        )


_CACHE = _KeyCache()


def key_id_for(key: bytes) -> bytes:
    if len(key) != KEY_LEN:
        raise LabelCryptoError(
            f"label key must be exactly {KEY_LEN} bytes, got {len(key)}"
        )
    return hashlib.sha256(key).digest()[:KEY_ID_LEN]


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise LabelCryptoError(
            "The cryptography package is required for label encryption. "
            "Install the project dependencies and try again."
        ) from exc
    return AESGCM(key)


def _envelope_aad(key_id: bytes) -> bytes:
    return MAGIC + bytes([VERSION]) + key_id


def _wrap_aad(purpose: bytes, dek_id: bytes, extra: bytes = b"") -> bytes:
    return b"XALK1" + bytes([CREDENTIAL_VERSION]) + purpose + dek_id + extra


def split_envelope(blob: bytes) -> EnvelopeParts:
    if len(blob) < HEADER_LEN + TAG_LEN:
        raise LabelCryptoError("truncated label envelope")
    if blob[:4] != MAGIC:
        raise LabelCryptoError("label file is not an XAL1 envelope")
    version = blob[4]
    if version != VERSION:
        raise LabelCryptoError(
            f"unsupported label envelope version: {version}"
        )
    key_id = blob[5 : 5 + KEY_ID_LEN]
    nonce = blob[5 + KEY_ID_LEN : HEADER_LEN]
    body = blob[HEADER_LEN:]
    if len(nonce) != NONCE_LEN or len(body) < TAG_LEN:
        raise LabelCryptoError("truncated label envelope")
    return EnvelopeParts(
        version=version, key_id=key_id, nonce=nonce, body=body
    )


def is_envelope(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def encrypt_bytes(
    plaintext: bytes,
    material: KeyMaterial,
    nonce: bytes | None = None,
) -> bytes:
    if not isinstance(plaintext, (bytes, bytearray)):
        raise LabelCryptoError("plaintext must be bytes")
    if nonce is None:
        nonce = secrets.token_bytes(NONCE_LEN)
    if len(nonce) != NONCE_LEN:
        raise LabelCryptoError(f"nonce must be {NONCE_LEN} bytes")
    if len(material.key_id) != KEY_ID_LEN:
        raise LabelCryptoError("key id must be 16 bytes")
    header = _envelope_aad(material.key_id) + nonce
    try:
        body = _aesgcm(material.key).encrypt(
            nonce, bytes(plaintext), _envelope_aad(material.key_id)
        )
    except LabelCryptoError:
        raise
    except Exception as exc:
        raise LabelCryptoError("failed to encrypt label file") from exc
    return header + body


def decrypt_bytes(blob: bytes, material: KeyMaterial) -> bytes:
    parts = split_envelope(blob)
    if parts.key_id != material.key_id:
        raise LabelCryptoError(
            "label file was encrypted with a different key "
            f"(key id {parts.key_id.hex()})"
        )
    try:
        return _aesgcm(material.key).decrypt(
            parts.nonce, parts.body, _envelope_aad(material.key_id)
        )
    except LabelCryptoError:
        raise
    except Exception as exc:
        raise LabelCryptoError(
            "label authentication failed; refusing to return plaintext"
        ) from exc


def load_key_file(path: str) -> KeyMaterial:
    key_path = osp.abspath(osp.expanduser(path))
    try:
        data = Path(key_path).read_bytes()
    except OSError as exc:
        raise LabelCryptoError(
            f"cannot read label key file: {key_path}"
        ) from exc
    if len(data) != KEY_LEN:
        raise LabelCryptoError(
            f"label key file must be exactly {KEY_LEN} bytes, "
            f"got {len(data)}: {key_path}"
        )
    return KeyMaterial(key=data, key_id=key_id_for(data), path=key_path)


def metadata_path(key_path: str) -> str:
    return key_path + METADATA_SUFFIX


def keystore_path(master_path: str) -> str:
    return osp.abspath(osp.expanduser(master_path)) + KEYSTORE_SUFFIX


def _write_new_file(path: str, data: bytes, mode: int) -> None:
    directory = osp.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise LabelCryptoError(f"refusing to overwrite {path}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _tighten_private_fd(fd: int) -> None:
    """Force mode 0600 on an open temp file when the OS can do it.

    ``os.fchmod`` is Unix-only. Windows raises ``AttributeError`` because
    the attribute is missing. ``mkstemp`` already requests mode 0600, and
    the caller still ``chmod``s the path after the replace. A missing
    ``fchmod`` is a no-op so the write can finish; it does not change
    encryption or who may decrypt.
    """

    fchmod = getattr(os, "fchmod", None)
    if not callable(fchmod):
        return
    fchmod(fd, 0o600)


def _write_private_replace(path: str, data: bytes) -> None:
    directory = osp.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = None
    fd = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=".xal_", suffix=".tmp", dir=directory
        )
        _tighten_private_fd(fd)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        temporary = None
    finally:
        if fd is not None:
            # Close before unlinking. If ``fchmod`` fails first, the
            # descriptor is still open and Windows refuses to delete the
            # temp file (WinError 32).
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary and osp.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


def generate_key_file(path: str) -> KeyMaterial:
    key_path = osp.abspath(osp.expanduser(path))
    if osp.exists(key_path):
        raise LabelCryptoError(
            f"refusing to overwrite existing key file: {key_path}"
        )
    key = secrets.token_bytes(KEY_LEN)
    material = KeyMaterial(key=key, key_id=key_id_for(key), path=key_path)
    _write_new_file(key_path, key, 0o600)
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {"key_id": material.key_id.hex(), "created_at": created_at}
    meta_bytes = json.dumps(meta, indent=2).encode("utf-8") + b"\n"
    if key.hex() in meta_bytes.decode("utf-8") or b'"key"' in meta_bytes:
        raise LabelCryptoError("refusing to write key bytes into metadata")
    try:
        _write_new_file(metadata_path(key_path), meta_bytes, 0o644)
    except Exception:
        try:
            os.remove(key_path)
        except OSError:
            pass
        raise
    return material


def _fail_cache(path: str | None, message: str) -> None:
    _CACHE.path = path
    _CACHE.credential_path = None
    _CACHE.master_path = None
    _CACHE.passphrase = None
    _CACHE.material = None
    _CACHE.active_id = None
    _CACHE.deks = {}
    _CACHE.role = None
    _CACHE.error = message
    _CACHE.loaded = True


def _install_deks(
    deks: dict,
    role: str,
    *,
    raw_path: str | None = None,
    credential_path: str | None = None,
    master_path: str | None = None,
    passphrase: str | None = None,
) -> KeyMaterial | None:
    _CACHE.path = raw_path
    _CACHE.credential_path = credential_path
    _CACHE.master_path = master_path
    _CACHE.passphrase = passphrase
    _CACHE.deks = dict(deks)
    _CACHE.role = role
    _CACHE.loaded = True
    _CACHE.error = None
    if len(deks) == 1:
        material = next(iter(deks.values()))
        _CACHE.active_id = material.key_id
        _CACHE.material = material
    else:
        _CACHE.active_id = None
        _CACHE.material = None
    return _CACHE.material


def _active_material() -> KeyMaterial | None:
    deks = _CACHE.deks or {}
    if _CACHE.active_id in deks:
        return deks[_CACHE.active_id]
    if len(deks) == 1:
        return next(iter(deks.values()))
    return None


def clear_key_cache() -> None:
    global _CACHE
    _CACHE = _KeyCache()


def current_key() -> KeyMaterial | None:
    if not _CACHE.loaded or _CACHE.error:
        return None
    return _CACHE.material


def current_role() -> str | None:
    if not _CACHE.loaded or _CACHE.error:
        return None
    return _CACHE.role


def training_export_allowed() -> bool:
    """Plaintext training export is allowed only for an algorithm session.

    An unlocked annotator credential and a process with no role both fail
    closed. Callers in the export menu, converters, and overview use this.
    """

    return current_role() == ROLE_ALGORITHM


def configure_key_file(path, *, force: bool = False) -> KeyMaterial | None:
    """Install a raw DEK for in-process encrypt/decrypt.

    The GUI settings path does not call this. A raw key session is the
    annotator role: labels can be edited, plaintext training export cannot.
    ``None`` or a blank path clears the in-memory key.
    """

    if path is None or (isinstance(path, str) and not str(path).strip()):
        normalized = None
    else:
        normalized = osp.abspath(osp.expanduser(str(path)))
    if (
        not force
        and _CACHE.loaded
        and _CACHE.error is None
        and _CACHE.path == normalized
        and _CACHE.role == ROLE_ANNOTATOR
        and _CACHE.master_path is None
    ):
        return _CACHE.material
    if normalized is None:
        clear_key_cache()
        return None
    try:
        material = load_key_file(normalized)
    except LabelCryptoError as exc:
        _fail_cache(normalized, str(exc))
        raise
    return _install_deks(
        {material.key_id: material},
        ROLE_ANNOTATOR,
        raw_path=normalized,
    )


def _clean_path(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def configure_from_config(config, *, passphrase=None, prompter=None):
    """Unlock a session from settings.

    A master that actually unwraps a DEK becomes the algorithm role.
    A master that unwraps nothing does not elevate; a ``.xalk`` passphrase
    session stays the annotator role. A leftover raw ``label_key_file``
    alone is refused.
    """

    if not isinstance(config, dict):
        raise LabelCryptoError("label settings are missing")
    master = _clean_path(config.get("label_master_key_file"))
    credential = _clean_path(config.get("label_credential_file"))
    legacy = _clean_path(config.get("label_key_file"))
    if master is None and credential is None:
        if legacy:
            raise LabelCryptoError(LEGACY_KEY_MESSAGE)
        clear_key_cache()
        return None
    if master:
        try:
            return configure_master(
                master, credential_path=credential, force=True
            )
        except NoWrappedDekError:
            if not credential:
                raise
    if not credential:
        raise LabelCryptoError(MISSING_KEY_MESSAGE)
    chosen = passphrase or os.environ.get("XLABEL_PASSPHRASE") or None
    if not chosen and prompter is not None:
        chosen = prompter()
    if not chosen:
        raise LabelCryptoError(PASSPHRASE_REQUIRED_MESSAGE)
    return configure_credential(credential, chosen, force=True)


def _validate_passphrase(passphrase: str) -> str:
    if not isinstance(passphrase, str):
        raise LabelCryptoError("passphrase must be text")
    if len(passphrase.strip()) < MIN_PASSPHRASE_LEN:
        raise LabelCryptoError(
            f"passphrase must be at least {MIN_PASSPHRASE_LEN} characters"
        )
    return passphrase


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(value, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise LabelCryptoError(f"credential {label} is missing")
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:
        raise LabelCryptoError(f"credential {label} is invalid") from exc


def _parse_id(value, label: str) -> bytes:
    if not isinstance(value, str):
        raise LabelCryptoError(f"credential {label} is missing")
    try:
        parsed = bytes.fromhex(value)
    except ValueError as exc:
        raise LabelCryptoError(f"credential {label} is invalid") from exc
    if len(parsed) != KEY_ID_LEN:
        raise LabelCryptoError(f"credential {label} is invalid")
    return parsed


def _require_mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise LabelCryptoError(f"credential {label} is missing")
    return value


def _dump_json(document: dict) -> bytes:
    return json.dumps(document, indent=2).encode("utf-8") + b"\n"


def _assert_no_secret(blob: bytes, secrets_to_hide: list[bytes]) -> None:
    for secret in secrets_to_hide:
        if not secret:
            continue
        if secret in blob or secret.hex().encode("ascii") in blob:
            raise LabelCryptoError("refusing to write key material")


def _seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    try:
        return _aesgcm(key).encrypt(nonce, plaintext, aad)
    except LabelCryptoError:
        raise
    except Exception as exc:
        raise LabelCryptoError("failed to wrap data key") from exc


def _unwrap_dek(key: bytes, nonce: bytes, body: bytes, aad: bytes) -> bytes:
    if len(nonce) != NONCE_LEN:
        raise LabelCryptoError("wrap nonce must be 12 bytes")
    try:
        dek = _aesgcm(key).decrypt(nonce, body, aad)
    except LabelCryptoError:
        raise
    except Exception as exc:
        raise LabelCryptoError(
            "credential authentication failed; refusing to return "
            "the data key"
        ) from exc
    if len(dek) != KEY_LEN:
        raise LabelCryptoError("unwrapped data key has the wrong length")
    return dek


def _material_from_dek(
    dek: bytes, path: str | None, declared_id: bytes
) -> KeyMaterial:
    material = KeyMaterial(key=dek, key_id=key_id_for(dek), path=path)
    if not hmac.compare_digest(material.key_id, declared_id):
        raise LabelCryptoError("wrapped data key does not match dek id")
    return material


def _derive_kek(passphrase: str, kdf: dict) -> bytes:
    if kdf.get("name") != "scrypt":
        raise LabelCryptoError("unsupported credential KDF")
    if (
        kdf.get("n") != SCRYPT_N
        or kdf.get("r") != SCRYPT_R
        or kdf.get("p") != SCRYPT_P
    ):
        raise LabelCryptoError("unexpected scrypt parameters")
    salt = _b64d(kdf.get("salt"), "scrypt salt")
    if len(salt) != 16:
        raise LabelCryptoError("invalid scrypt salt")
    try:
        return hashlib.scrypt(
            passphrase.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=KEY_LEN,
            maxmem=64 * 1024 * 1024,
        )
    except LabelCryptoError:
        raise
    except Exception as exc:
        raise LabelCryptoError("failed to derive credential key") from exc


def _is_credential_blob(path: str, raw: bytes) -> bool:
    if str(path).lower().endswith(CREDENTIAL_SUFFIX):
        return True
    stripped = raw.lstrip()
    return stripped.startswith(b"{") and b'"XALK1"' in raw[:4096]


def load_credential_document(path: str) -> dict:
    key_path = osp.abspath(osp.expanduser(path))
    try:
        raw = Path(key_path).read_bytes()
    except OSError as exc:
        raise LabelCryptoError(
            f"cannot read annotator credential: {key_path}"
        ) from exc
    if len(raw) == KEY_LEN and not raw.lstrip().startswith(b"{"):
        raise LabelCryptoError(RAW_KEY_AS_CREDENTIAL)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LabelCryptoError(
            f"not an annotator credential: {key_path}"
        ) from exc
    if not isinstance(document, dict):
        raise LabelCryptoError(f"not an annotator credential: {key_path}")
    if document.get("format") != CREDENTIAL_FORMAT:
        raise LabelCryptoError(f"not an annotator credential: {key_path}")
    version = document.get("version")
    if version != CREDENTIAL_VERSION:
        raise LabelCryptoError(
            f"unsupported annotator credential version: {version}"
        )
    if document.get("role") != ROLE_ANNOTATOR:
        raise LabelCryptoError("credential role is not annotator")
    return document


def _unwrap_passphrase(document: dict, passphrase: str, path: str):
    passphrase = _validate_passphrase(passphrase)
    dek_id = _parse_id(document.get("dek_id"), "dek id")
    kdf = _require_mapping(document.get("kdf"), "kdf")
    wrap = _require_mapping(document.get("passphrase_wrap"), "passphrase wrap")
    kek = _derive_kek(passphrase, kdf)
    nonce = _b64d(wrap.get("nonce"), "passphrase nonce")
    body = _b64d(wrap.get("ciphertext"), "passphrase ciphertext")
    dek = _unwrap_dek(kek, nonce, body, _wrap_aad(b"passphrase", dek_id))
    return _material_from_dek(dek, path, dek_id)


def _unwrap_credential_master(path: str, master: KeyMaterial):
    document = load_credential_document(path)
    wrap = _require_mapping(document.get("master_wrap"), "master wrap")
    master_id = _parse_id(wrap.get("master_id"), "master id")
    if len(master_id) != len(master.key_id):
        return None
    if not hmac.compare_digest(master_id, master.key_id):
        return None
    dek_id = _parse_id(document.get("dek_id"), "dek id")
    nonce = _b64d(wrap.get("nonce"), "master nonce")
    body = _b64d(wrap.get("ciphertext"), "master ciphertext")
    dek = _unwrap_dek(
        master.key,
        nonce,
        body,
        _wrap_aad(b"master", dek_id, master.key_id),
    )
    return _material_from_dek(dek, None, dek_id)


def _load_keystore_document(path: str, master: KeyMaterial) -> dict:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise LabelCryptoError(f"cannot read keystore: {path}") from exc
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LabelCryptoError(f"keystore is not valid JSON: {path}") from exc
    if (
        not isinstance(document, dict)
        or document.get("format") != KEYSTORE_FORMAT
        or document.get("version") != 1
    ):
        raise LabelCryptoError(f"unsupported keystore: {path}")
    if document.get("master_id") != master.key_id.hex():
        raise LabelCryptoError(
            "keystore was wrapped for a different master key"
        )
    if not isinstance(document.get("entries"), list):
        raise LabelCryptoError("keystore entries are invalid")
    return document


def _unwrap_store_entry(entry, master: KeyMaterial) -> KeyMaterial:
    if not isinstance(entry, dict):
        raise LabelCryptoError("keystore entry is invalid")
    dek_id = _parse_id(entry.get("dek_id"), "dek id")
    nonce = _b64d(entry.get("nonce"), "keystore nonce")
    body = _b64d(entry.get("ciphertext"), "keystore ciphertext")
    dek = _unwrap_dek(
        master.key,
        nonce,
        body,
        _wrap_aad(b"keystore", dek_id, master.key_id),
    )
    return _material_from_dek(dek, None, dek_id)


def _unwrap_store_entries(document: dict, master: KeyMaterial) -> dict:
    deks = {}
    for entry in document["entries"]:
        material = _unwrap_store_entry(entry, master)
        deks[material.key_id] = material
    return deks


def _remember_dek(deks: dict, material: KeyMaterial) -> None:
    previous = deks.get(material.key_id)
    if previous is not None and not hmac.compare_digest(
        previous.key, material.key
    ):
        raise LabelCryptoError(
            "credential data key does not match the master keystore"
        )
    deks[material.key_id] = material


def _open_master_deks(
    master_path: str,
    credential_path: str | None = None,
    *,
    require_credential: bool = False,
):
    path = osp.abspath(osp.expanduser(master_path))
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise LabelCryptoError(f"cannot read label key file: {path}") from exc
    if _is_credential_blob(path, raw):
        raise LabelCryptoError(DECRYPT_REJECTS_CREDENTIAL)
    master = load_key_file(path)
    deks = {}
    store = keystore_path(master.path)
    if osp.isfile(store):
        document = _load_keystore_document(store, master)
        deks.update(_unwrap_store_entries(document, master))
    if credential_path:
        extra = _unwrap_credential_master(credential_path, master)
        if extra is None and (require_credential or not deks):
            if require_credential:
                raise LabelCryptoError(
                    "credential master wrap does not match this master key"
                )
        elif extra is not None:
            _remember_dek(deks, extra)
    if not deks:
        raise NoWrappedDekError(NO_WRAPPED_DEK_MESSAGE)
    return master, deks


def _upsert_keystore(master: KeyMaterial, dek: KeyMaterial) -> str:
    path = keystore_path(master.path)
    if osp.isfile(path):
        document = _load_keystore_document(path, master)
        current = _unwrap_store_entries(document, master)
        existing = current.get(dek.key_id)
        if existing is not None and not hmac.compare_digest(
            existing.key, dek.key
        ):
            raise LabelCryptoError(
                "refusing to replace the wrapped data key "
                f"for dek id {dek.key_id.hex()}"
            )
    else:
        document = {
            "format": KEYSTORE_FORMAT,
            "version": 1,
            "master_id": master.key_id.hex(),
            "entries": [],
        }
    nonce = secrets.token_bytes(NONCE_LEN)
    body = _seal(
        master.key,
        nonce,
        dek.key,
        _wrap_aad(b"keystore", dek.key_id, master.key_id),
    )
    entries = [
        entry
        for entry in document["entries"]
        if not (
            isinstance(entry, dict) and entry.get("dek_id") == dek.key_id.hex()
        )
    ]
    entries.append(
        {
            "dek_id": dek.key_id.hex(),
            "nonce": _b64e(nonce),
            "ciphertext": _b64e(body),
        }
    )
    document["entries"] = entries
    blob = _dump_json(document)
    _assert_no_secret(blob, [master.key, dek.key])
    _write_private_replace(path, blob)
    return path


def wrap_credential(
    master_path: str,
    output: str,
    passphrase: str,
    dek_path: str | None = None,
) -> KeyMaterial:
    """Write a ``.xalk`` and update ``<master>.xalstore``.

    ``dek_path`` is an existing 32-byte DEK (the old project key). When it
    is omitted a new DEK is generated and the raw bytes are not written.
    """

    passphrase = _validate_passphrase(passphrase)
    master = load_key_file(master_path)
    output_path = osp.abspath(osp.expanduser(output))
    if osp.exists(output_path):
        raise LabelCryptoError(f"refusing to overwrite {output_path}")
    if dek_path:
        dek = load_key_file(dek_path).key
    else:
        dek = secrets.token_bytes(KEY_LEN)
    dek_material = KeyMaterial(key=dek, key_id=key_id_for(dek), path=None)
    salt = secrets.token_bytes(16)
    kek = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_LEN,
        maxmem=64 * 1024 * 1024,
    )
    pass_nonce = secrets.token_bytes(NONCE_LEN)
    master_nonce = secrets.token_bytes(NONCE_LEN)
    document = {
        "format": CREDENTIAL_FORMAT,
        "version": CREDENTIAL_VERSION,
        "role": ROLE_ANNOTATOR,
        "dek_id": dek_material.key_id.hex(),
        "kdf": {
            "name": "scrypt",
            "salt": _b64e(salt),
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
        },
        "passphrase_wrap": {
            "nonce": _b64e(pass_nonce),
            "ciphertext": _b64e(
                _seal(
                    kek,
                    pass_nonce,
                    dek,
                    _wrap_aad(b"passphrase", dek_material.key_id),
                )
            ),
        },
        "master_wrap": {
            "master_id": master.key_id.hex(),
            "nonce": _b64e(master_nonce),
            "ciphertext": _b64e(
                _seal(
                    master.key,
                    master_nonce,
                    dek,
                    _wrap_aad(b"master", dek_material.key_id, master.key_id),
                )
            ),
        },
    }
    blob = _dump_json(document)
    _assert_no_secret(blob, [dek, master.key, passphrase.encode("utf-8")])
    _upsert_keystore(master, dek_material)
    _write_new_file(output_path, blob, 0o600)
    return dek_material


def configure_credential(
    path: str, passphrase: str, *, force: bool = False
) -> KeyMaterial:
    normalized = osp.abspath(osp.expanduser(path))
    passphrase = _validate_passphrase(passphrase)
    if (
        not force
        and _CACHE.loaded
        and _CACHE.error is None
        and _CACHE.role == ROLE_ANNOTATOR
        and _CACHE.credential_path == normalized
        and _CACHE.passphrase == passphrase
        and _CACHE.material is not None
    ):
        return _CACHE.material
    document = load_credential_document(normalized)
    material = _unwrap_passphrase(document, passphrase, normalized)
    installed = _install_deks(
        {material.key_id: material},
        ROLE_ANNOTATOR,
        credential_path=normalized,
        passphrase=passphrase,
    )
    return installed


def configure_master(
    master_path: str,
    credential_path: str | None = None,
    *,
    force: bool = False,
) -> KeyMaterial | None:
    normalized = osp.abspath(osp.expanduser(master_path))
    credential = (
        osp.abspath(osp.expanduser(credential_path))
        if credential_path
        else None
    )
    if (
        not force
        and _CACHE.loaded
        and _CACHE.error is None
        and _CACHE.role == ROLE_ALGORITHM
        and _CACHE.master_path == normalized
        and _CACHE.credential_path == credential
        and _CACHE.deks
    ):
        return _active_material()
    _master, deks = _open_master_deks(normalized, credential)
    _install_deks(
        deks,
        ROLE_ALGORITHM,
        master_path=normalized,
        credential_path=credential,
    )
    return _active_material()


def require_key() -> KeyMaterial:
    """Return the active DEK, re-reading after a failed load."""

    material = _active_material()
    if material is not None and not _CACHE.error:
        _CACHE.material = material
        _CACHE.active_id = material.key_id
        return material
    if _CACHE.master_path:
        configure_master(
            _CACHE.master_path,
            credential_path=_CACHE.credential_path,
            force=True,
        )
    elif _CACHE.credential_path:
        if not _CACHE.passphrase:
            raise LabelCryptoError(PASSPHRASE_REQUIRED_MESSAGE)
        configure_credential(
            _CACHE.credential_path, _CACHE.passphrase, force=True
        )
    elif _CACHE.path:
        return configure_key_file(_CACHE.path, force=True)
    else:
        raise LabelCryptoError(MISSING_KEY_MESSAGE)
    material = _active_material()
    if material is None:
        if _CACHE.deks and len(_CACHE.deks) > 1:
            raise LabelCryptoError(
                "multiple data keys are unlocked; open a label from this "
                "project before saving"
            )
        raise LabelCryptoError(MISSING_KEY_MESSAGE)
    _CACHE.material = material
    _CACHE.active_id = material.key_id
    return material


def _dek_by_id(dek_id: bytes) -> KeyMaterial:
    if not _CACHE.deks:
        require_key()
    material = (_CACHE.deks or {}).get(dek_id)
    if material is not None:
        return material
    if not _CACHE.deks:
        raise LabelCryptoError(MISSING_KEY_MESSAGE)
    raise LabelCryptoError(
        "label file was encrypted with a different key "
        f"(key id {dek_id.hex()})"
    )


def decrypt_label_blob(blob: bytes) -> bytes:
    parts = split_envelope(blob)
    material = _dek_by_id(parts.key_id)
    plaintext = decrypt_bytes(blob, material)
    _CACHE.active_id = material.key_id
    _CACHE.material = material
    return plaintext


def encrypt_label_blob(plaintext: bytes) -> bytes:
    return encrypt_bytes(plaintext, require_key())


def _refuse_existing(path: Path) -> None:
    if path.exists():
        raise LabelCryptoError(f"refusing to overwrite existing file: {path}")


def _atomic_write(path: str, data: bytes) -> None:
    _refuse_existing(Path(path))
    directory = osp.dirname(osp.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=".xal_", suffix=".tmp", dir=directory
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary and osp.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


def _decrypt_with_deks(deks: dict, source: Path, output: Path) -> None:
    blob = source.read_bytes()
    parts = split_envelope(blob)
    material = deks.get(parts.key_id)
    if material is None:
        raise LabelCryptoError(
            "label file was encrypted with a different key "
            f"(key id {parts.key_id.hex()})"
        )
    _atomic_write(str(output), decrypt_bytes(blob, material))


def _encrypt_file(material: KeyMaterial, source: Path, output: Path) -> None:
    plaintext = source.read_bytes()
    _atomic_write(str(output), encrypt_bytes(plaintext, material))


def _iter_encrypted_files(directory: Path):
    files = [
        path
        for path in directory.rglob(f"*{ENCRYPTED_SUFFIX}")
        if path.is_file()
    ]
    return sorted(files)


def _output_pairs(src: Path, dst: Path):
    if src.is_dir():
        if dst.exists() and not dst.is_dir():
            raise LabelCryptoError(
                f"batch decrypt output must be a directory: {dst}"
            )
        files = _iter_encrypted_files(src)
        if not files:
            raise LabelCryptoError(f"no {ENCRYPTED_SUFFIX} files in {src}")
        pairs = []
        for path in files:
            target = dst / path.relative_to(src).with_suffix(".json")
            _refuse_existing(target)
            pairs.append((path, target))
        return pairs
    if not src.is_file():
        raise LabelCryptoError(f"input not found: {src}")
    if dst.exists() and dst.is_dir():
        dst = dst / (src.stem + ".json")
    _refuse_existing(dst)
    return [(src, dst)]


def _select_dek(deks: dict, dek_id: str | None) -> KeyMaterial:
    if dek_id:
        try:
            parsed = bytes.fromhex(dek_id)
        except ValueError as exc:
            raise LabelCryptoError("--dek-id must be hex") from exc
        material = deks.get(parsed)
        if material is None:
            raise LabelCryptoError(
                f"dek id {dek_id} is not in the master keystore"
            )
        return material
    if len(deks) == 1:
        return next(iter(deks.values()))
    available = ", ".join(item.key_id.hex() for item in deks.values())
    raise LabelCryptoError(
        "multiple data keys are unlocked; pass --dek-id. "
        f"Available: {available}"
    )


def decrypt_tree(
    key_path: str,
    source: str,
    output: str,
    credential: str | None = None,
) -> int:
    _master, deks = _open_master_deks(
        key_path,
        credential,
        require_credential=bool(credential),
    )
    pairs = _output_pairs(Path(source), Path(output))
    for path, target in pairs:
        target.parent.mkdir(parents=True, exist_ok=True)
        _decrypt_with_deks(deks, path, target)
    return len(pairs)


def encrypt_file(
    key_path: str,
    source: str,
    output: str,
    dek_id: str | None = None,
) -> None:
    _master, deks = _open_master_deks(key_path, None)
    material = _select_dek(deks, dek_id)
    src = Path(source)
    dst = Path(output)
    if not src.is_file():
        raise LabelCryptoError(f"input file not found: {src}")
    if dst.exists() and dst.is_dir():
        dst = dst / (src.stem + ENCRYPTED_SUFFIX)
    _refuse_existing(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _encrypt_file(material, src, dst)


def _run(action) -> int:
    try:
        action()
    except LabelCryptoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _passphrase_from_file(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LabelCryptoError(f"cannot read passphrase file: {path}") from exc
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    return _validate_passphrase(text)


def _prompt_passphrase() -> str:
    import getpass

    first = getpass.getpass("Annotator passphrase: ")
    second = getpass.getpass("Confirm passphrase: ")
    if first != second:
        raise LabelCryptoError("passphrases do not match")
    return _validate_passphrase(first)


def resolve_passphrase(passphrase_file: str | None) -> str:
    if passphrase_file:
        return _passphrase_from_file(passphrase_file)
    env = os.environ.get("XLABEL_PASSPHRASE")
    if env:
        return _validate_passphrase(env)
    if sys.stdin is not None and sys.stdin.isatty():
        return _prompt_passphrase()
    raise LabelCryptoError(
        "passphrase required: set XLABEL_PASSPHRASE, pass "
        "--passphrase-file, or run xlabel-wrap in a terminal"
    )


def keygen_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="xlabel-keygen",
        description=(
            "Generate a 32-byte master key. Annotator machines do not use "
            "this file; wrap a data key for them with xlabel-wrap. The "
            "same command can also create a fresh DEK file for xlabel-wrap "
            "--dek when there is no existing project key."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="path of the raw key file to create",
    )
    args = parser.parse_args(argv)

    def action():
        material = generate_key_file(args.output)
        print(material.key_id.hex())

    return _run(action)


def decrypt_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="xlabel-decrypt",
        description=(
            "Decrypt .xalenc labels with a master key. The DEK is unwrapped "
            "from <master>.xalstore (written by xlabel-wrap). A .xalk "
            "credential or a passphrase is rejected. Existing output files "
            "are left untouched."
        ),
    )
    parser.add_argument(
        "-k",
        "--key",
        required=True,
        help="master key file from xlabel-keygen (not a .xalk credential)",
    )
    parser.add_argument(
        "--credential",
        help=(
            "optional .xalk whose master_wrap slot is opened with the "
            "master key. The passphrase is ignored"
        ),
    )
    parser.add_argument("input", help="an .xalenc file or a directory")
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help=(
            "output .json file, or a directory for batch mode; "
            "existing files are left untouched"
        ),
    )
    args = parser.parse_args(argv)
    return _run(
        lambda: decrypt_tree(
            args.key, args.input, args.output, credential=args.credential
        )
    )


def encrypt_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="xlabel-encrypt",
        description=(
            "Encrypt a plaintext label with the DEK wrapped for a master "
            "key. Annotator .xalk files are not accepted."
        ),
    )
    parser.add_argument(
        "-k",
        "--key",
        required=True,
        help="master key file from xlabel-keygen",
    )
    parser.add_argument(
        "--dek-id",
        help="DEK id when the master keystore holds more than one",
    )
    parser.add_argument("input", help="plaintext label file")
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="output .xalenc file; an existing file is left untouched",
    )
    args = parser.parse_args(argv)
    return _run(
        lambda: encrypt_file(
            args.key, args.input, args.output, dek_id=args.dek_id
        )
    )


def wrap_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="xlabel-wrap",
        description=(
            "Wrap a data key into an annotator .xalk credential and into "
            "<master>.xalstore. Pass --dek to reuse an existing project "
            "key so current .xalenc files do not need to be rewritten. "
            "Refuses to overwrite the .xalk output."
        ),
    )
    parser.add_argument(
        "--master",
        required=True,
        help="32-byte master key from xlabel-keygen",
    )
    parser.add_argument(
        "--dek",
        help=(
            "existing 32-byte DEK (old project.key). Omit to generate a "
            "new DEK and keep the raw bytes off disk"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="annotator .xalk to create; an existing file is left untouched",
    )
    parser.add_argument(
        "--passphrase-file",
        help=(
            "file containing the annotator passphrase. Otherwise "
            "XLABEL_PASSPHRASE or an interactive prompt is used"
        ),
    )
    args = parser.parse_args(argv)

    def action():
        passphrase = resolve_passphrase(args.passphrase_file)
        material = wrap_credential(
            args.master,
            args.output,
            passphrase,
            dek_path=args.dek,
        )
        print(material.key_id.hex())

    return _run(action)
