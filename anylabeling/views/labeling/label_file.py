import base64
import json
import os
import os.path as osp
import tempfile

from PIL import ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

from anylabeling.label_crypto import (
    ENCRYPTED_SUFFIX,
    MAGIC,
    LabelCryptoError,
    decrypt_bytes,
    encrypt_bytes,
    require_key,
)

from . import utils
from .label_converter import LabelConverter
from .logger import logger
from .schema import XLABEL_BASIC_FIELDS, create_xlabel_template
from .shape import Shape

LEGACY_SUFFIX = ".json"


class LabelFileError(Exception):
    pass


def serialize_label(data) -> bytes:
    """UTF-8 bytes ``json.dump(..., ensure_ascii=False, indent=2)`` writes."""

    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def encrypted_output_path(filename: str) -> str:
    root, ext = osp.splitext(filename)
    if ext.lower() == ENCRYPTED_SUFFIX:
        return filename
    return root + ENCRYPTED_SUFFIX


def _is_encrypted_payload(filename: str, raw: bytes) -> bool:
    if raw.startswith(MAGIC):
        return True
    return str(filename).lower().endswith(ENCRYPTED_SUFFIX)


class LabelFile:
    suffix = ENCRYPTED_SUFFIX
    legacy_suffix = LEGACY_SUFFIX

    def __init__(self, filename=None, image_dir=None):
        self.shapes = []
        self.image_path = None
        self.image_data = None
        self.image_dir = image_dir
        # Always expose a complete attribute set, even when the instance is
        # created without a filename (e.g. LabelFile() in save_labels):
        # callers may read .flags / .other_data before any load() happens.
        self.flags = {}
        self.other_data = {}
        if filename is not None:
            self.load(filename)
        self.filename = filename

    @staticmethod
    def _check_image_height_and_width(image_data, image_height, image_width):
        actual_width, actual_height = utils.get_pil_img_dim(image_data)
        if image_height is not None and actual_height != image_height:
            logger.error(
                "image_height does not match with image_data or image_path, "
                "so getting image_height from actual image."
            )
            image_height = actual_height
        if image_width is not None and actual_width != image_width:
            logger.error(
                "image_width does not match with image_data or image_path, "
                "so getting image_width from actual image."
            )
            image_width = actual_width
        return image_height, image_width

    @staticmethod
    def is_label_file(filename):
        ext = osp.splitext(filename)[1].lower()
        return ext in {LabelFile.suffix, LabelFile.legacy_suffix}

    @staticmethod
    def resolve_label_path(directory, stem):
        """Existing encrypted file, else legacy JSON, else the save path."""

        encrypted = osp.join(directory, stem + ENCRYPTED_SUFFIX)
        legacy = osp.join(directory, stem + LEGACY_SUFFIX)
        if osp.isfile(encrypted):
            return encrypted
        if osp.isfile(legacy):
            return legacy
        return encrypted

    @staticmethod
    def path_for_image(image_path, output_dir=None):
        directory = output_dir or osp.dirname(image_path)
        stem = osp.splitext(osp.basename(image_path))[0]
        return LabelFile.resolve_label_path(directory, stem)

    @staticmethod
    def remove_label_pair(path):
        if not path:
            return
        root, _ext = osp.splitext(path)
        for candidate in (root + ENCRYPTED_SUFFIX, root + LEGACY_SUFFIX):
            if osp.isfile(candidate):
                os.remove(candidate)

    @staticmethod
    def load_image_file(filename, default=None):
        try:
            with open(filename, "rb") as f:
                return f.read()
        except Exception:
            logger.error(f"Failed opening image file: {filename}")
            return default

    @staticmethod
    def read_document(filename):
        with open(filename, "rb") as handle:
            raw = handle.read()
        return LabelFile.decode_document(filename, raw)

    @staticmethod
    def decode_document(filename, raw: bytes):
        if _is_encrypted_payload(filename, raw):
            try:
                raw = decrypt_bytes(raw, require_key())
            except LabelCryptoError as exc:
                raise LabelFileError(exc) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise LabelFileError(exc) from exc

    @staticmethod
    def write_document(filename, data) -> str:
        """Encrypt ``data`` and write ``.xalenc`` only.

        A same-stem plaintext ``.json`` is removed after the encrypted file
        is in place, so a save does not leave the previous plaintext behind.
        """

        payload = serialize_label(data)
        try:
            blob = encrypt_bytes(payload, require_key())
        except LabelCryptoError as exc:
            raise LabelFileError(exc) from exc
        output = encrypted_output_path(filename)
        LabelFile._atomic_write_bytes(output, blob)
        legacy = osp.splitext(output)[0] + LEGACY_SUFFIX
        if osp.isfile(legacy) and osp.abspath(legacy) != osp.abspath(output):
            try:
                os.remove(legacy)
            except OSError as exc:
                raise LabelFileError(exc) from exc
        return output

    @staticmethod
    def _atomic_write_bytes(filename, payload: bytes):
        temporary_file = None
        try:
            directory = osp.dirname(osp.abspath(filename))
            fd, temporary_file = tempfile.mkstemp(
                prefix=".xal_",
                suffix=".tmp",
                dir=directory,
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_file, filename)
            temporary_file = None
        except Exception as exc:
            raise LabelFileError(exc) from exc
        finally:
            if temporary_file and osp.exists(temporary_file):
                try:
                    os.remove(temporary_file)
                except OSError:
                    pass

    def load(self, filename):
        try:
            data = self.read_document(filename)

            if data.get("version") is None:
                logger.warning(
                    f"Loading JSON file ({filename}) of unknown version"
                )

            if data["shapes"]:
                for i in range(len(data["shapes"])):
                    shape_points = data["shapes"][i]["points"]
                    if (
                        data["shapes"][i]["shape_type"] == "rectangle"
                        and len(shape_points) == 2
                    ):
                        logger.warning(
                            "UserWarning: Diagonal vertex mode is deprecated in X-AnyLabeling release v2.2.0 or later.\n"
                            "Please update your code to accommodate the new four-point mode."
                        )
                        data["shapes"][i]["points"] = (
                            utils.rectangle_from_diagonal(shape_points)
                        )

            data["imagePath"] = osp.basename(data["imagePath"])
            if data["imageData"] is not None:
                image_data = base64.b64decode(data["imageData"])
            else:
                # relative path from label file to relative path from cwd
                if self.image_dir:
                    image_path = osp.join(self.image_dir, data["imagePath"])
                else:
                    image_path = osp.join(
                        osp.dirname(filename), data["imagePath"]
                    )
                image_data = self.load_image_file(image_path)

            flags = data.get("flags", {})
            image_path = data["imagePath"]

            utils.get_pil_img_dim(image_data)

            shapes = [Shape().load_from_dict(s) for s in data["shapes"]]

        except LabelFileError:
            raise
        except Exception as e:  # noqa
            raise LabelFileError(e) from e

        other_data = {}
        for key, value in data.items():
            if key not in XLABEL_BASIC_FIELDS:
                other_data[key] = value

        # Add new fields if not available
        other_data["description"] = other_data.get("description", "")
        other_data["checked"] = data.get("checked", False) is True

        # Only replace data after everything is loaded.
        self.flags = flags
        self.shapes = shapes
        self.image_path = image_path
        self.image_data = image_data
        self.filename = filename
        self.other_data = other_data

    def save(
        self,
        filename=None,
        shapes=None,
        image_path=None,
        image_height=None,
        image_width=None,
        image_data=None,
        other_data=None,
        flags=None,
    ):
        if image_data is not None:
            image_height, image_width = self._check_image_height_and_width(
                image_data, image_height, image_width
            )
            image_data = base64.b64encode(image_data).decode("utf-8")

        if other_data is None:
            other_data = {}
        if flags is None:
            flags = {}
        checked = other_data.get("checked", False) is True
        for i, shape in enumerate(shapes):
            if shape["shape_type"] == "rectangle":
                sorted_box = LabelConverter.calculate_bounding_box(
                    shape["points"]
                )
                xmin, ymin, xmax, ymax = sorted_box
                shape["points"] = [
                    [xmin, ymin],
                    [xmax, ymin],
                    [xmax, ymax],
                    [xmin, ymax],
                ]
                shapes[i] = shape

        data = create_xlabel_template(
            flags=flags,
            checked=checked,
            shapes=shapes,
            image_path=image_path,
            image_data=image_data,
            image_height=image_height,
            image_width=image_width,
        )

        for key, value in other_data.items():
            if key == "checked":
                continue
            assert key not in data
            data[key] = value
        try:
            self.filename = self.write_document(filename, data)
        except LabelFileError:
            raise
        except Exception as e:  # noqa
            raise LabelFileError(e) from e
