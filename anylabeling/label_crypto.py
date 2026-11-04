"""AES-256-GCM envelope for X-AnyLabeling label files.

Envelope layout (``XAL1``):

    offset 0  : 4 bytes  magic ``b"XAL1"``
    offset 4  : 1 byte   version (currently ``1``)
    offset 5  : 16 bytes key id, ``SHA-256(key)[:16]``
    offset 21 : 12 bytes random nonce
    offset 33 : AES-256-GCM ciphertext with the 16-byte tag appended

The GCM associated data is ``magic || version || key_id``, so a modified
header fails closed. The plaintext is the xlabel JSON UTF-8 bytes that
``LabelFile`` would previously write with ``json.dump``.

The raw key is 32 bytes in a key file. A sidecar ``<key>.meta.json`` may
store ``key_id`` and ``created_at`` only. Key bytes are never written to
that sidecar, to logs, or into the application package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import os.path as osp
import secrets
import sys
import tempfile
from dataclasses import dataclass
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

MISSING_KEY_MESSAGE = (
    "Label encryption key is not configured. Set label_key_file to a "
    "32-byte key file created by xlabel-keygen. Refusing to write plaintext."
)


class LabelCryptoError(Exception):
    """Encryption or decryption failed closed."""


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
    material: KeyMaterial | None = None
    error: str | None = None
    loaded: bool = False


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


def _aad(key_id: bytes) -> bytes:
    return MAGIC + bytes([VERSION]) + key_id


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
    header = _aad(material.key_id) + nonce
    try:
        body = _aesgcm(material.key).encrypt(
            nonce, bytes(plaintext), _aad(material.key_id)
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
            parts.nonce, parts.body, _aad(material.key_id)
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


def configure_key_file(path, *, force: bool = False) -> KeyMaterial | None:
    """Load ``path`` into memory once.

    Passing the same path again is a no-op unless ``force`` is set. ``None``
    or a blank path clears the in-memory key. The key bytes stay in this
    process only.
    """

    if path is None or (isinstance(path, str) and not str(path).strip()):
        normalized = None
    else:
        normalized = osp.abspath(osp.expanduser(str(path)))
    if not force and _CACHE.loaded and _CACHE.path == normalized:
        if _CACHE.error:
            raise LabelCryptoError(_CACHE.error)
        return _CACHE.material
    _CACHE.path = normalized
    _CACHE.loaded = True
    _CACHE.material = None
    _CACHE.error = None
    if normalized is None:
        return None
    try:
        _CACHE.material = load_key_file(normalized)
    except LabelCryptoError as exc:
        _CACHE.error = str(exc)
        raise
    return _CACHE.material


def clear_key_cache() -> None:
    global _CACHE
    _CACHE = _KeyCache()


def current_key() -> KeyMaterial | None:
    if not _CACHE.loaded or _CACHE.error:
        return None
    return _CACHE.material


def require_key() -> KeyMaterial:
    if _CACHE.error:
        raise LabelCryptoError(_CACHE.error)
    if _CACHE.material is None:
        raise LabelCryptoError(MISSING_KEY_MESSAGE)
    return _CACHE.material


def _atomic_write(path: str, data: bytes) -> None:
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


def _decrypt_file(material: KeyMaterial, source: Path, output: Path) -> None:
    blob = source.read_bytes()
    plaintext = decrypt_bytes(blob, material)
    _atomic_write(str(output), plaintext)


def _encrypt_file(material: KeyMaterial, source: Path, output: Path) -> None:
    plaintext = source.read_bytes()
    blob = encrypt_bytes(plaintext, material)
    _atomic_write(str(output), blob)


def _iter_encrypted_files(directory: Path):
    files = [
        path
        for path in directory.rglob(f"*{ENCRYPTED_SUFFIX}")
        if path.is_file()
    ]
    return sorted(files)


def decrypt_tree(key_path: str, source: str, output: str) -> int:
    material = load_key_file(key_path)
    src = Path(source)
    dst = Path(output)
    if src.is_dir():
        if dst.exists() and not dst.is_dir():
            raise LabelCryptoError(
                f"batch decrypt output must be a directory: {dst}"
            )
        files = _iter_encrypted_files(src)
        if not files:
            raise LabelCryptoError(f"no {ENCRYPTED_SUFFIX} files in {src}")
        for path in files:
            relative = path.relative_to(src).with_suffix(".json")
            target = dst / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _decrypt_file(material, path, target)
        return len(files)
    if not src.is_file():
        raise LabelCryptoError(f"input not found: {src}")
    if dst.exists() and dst.is_dir():
        dst = dst / (src.stem + ".json")
    dst.parent.mkdir(parents=True, exist_ok=True)
    _decrypt_file(material, src, dst)
    return 1


def encrypt_file(key_path: str, source: str, output: str) -> None:
    material = load_key_file(key_path)
    src = Path(source)
    dst = Path(output)
    if not src.is_file():
        raise LabelCryptoError(f"input file not found: {src}")
    if dst.exists() and dst.is_dir():
        dst = dst / (src.stem + ENCRYPTED_SUFFIX)
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


def keygen_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="xlabel-keygen",
        description="Generate a 32-byte X-AnyLabeling label key file.",
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
        description="Decrypt an .xalenc label file or a directory of them.",
    )
    parser.add_argument("-k", "--key", required=True, help="32-byte key file")
    parser.add_argument("input", help="an .xalenc file or a directory")
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="output .json file, or a directory for batch mode",
    )
    args = parser.parse_args(argv)
    return _run(lambda: decrypt_tree(args.key, args.input, args.output))


def encrypt_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="xlabel-encrypt",
        description="Encrypt a plaintext label file into an .xalenc envelope.",
    )
    parser.add_argument("-k", "--key", required=True, help="32-byte key file")
    parser.add_argument("input", help="plaintext label file")
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="output .xalenc file",
    )
    args = parser.parse_args(argv)
    return _run(lambda: encrypt_file(args.key, args.input, args.output))
