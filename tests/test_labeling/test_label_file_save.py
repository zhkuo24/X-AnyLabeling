import base64
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

from anylabeling.label_crypto import (
    clear_key_cache,
    configure_key_file,
    decrypt_bytes,
    generate_key_file,
)
from anylabeling.views.labeling import label_file


class TestLabelFileSave(unittest.TestCase):
    def setUp(self):
        clear_key_cache()
        self.addCleanup(clear_key_cache)

    def test_image_dimensions_are_read_without_pixel_conversion(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (2, 3), "white").save(image_buffer, format="PNG")

        with tempfile.TemporaryDirectory() as directory:
            key = generate_key_file(os.path.join(directory, "project.key"))
            configure_key_file(key.path)
            filename = os.path.join(directory, "annotation.json")
            label = label_file.LabelFile()
            with mock.patch.object(
                label_file.utils,
                "img_data_to_arr",
                side_effect=AssertionError("unexpected pixel conversion"),
            ):
                label.save(
                    filename=filename,
                    shapes=[],
                    image_path="image.png",
                    image_height=1,
                    image_width=1,
                    image_data=image_buffer.getvalue(),
                )

            encrypted = os.path.join(directory, "annotation.xalenc")
            self.assertTrue(os.path.isfile(encrypted))
            self.assertFalse(os.path.exists(filename))
            data = json.loads(
                decrypt_bytes(open(encrypted, "rb").read(), key).decode(
                    "utf-8"
                )
            )
            self.assertEqual(data["imageHeight"], 3)
            self.assertEqual(data["imageWidth"], 2)
            self.assertEqual(
                base64.b64decode(data["imageData"]), image_buffer.getvalue()
            )

    def test_failed_save_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key = generate_key_file(os.path.join(directory, "project.key"))
            configure_key_file(key.path)
            filename = os.path.join(directory, "annotation.xalenc")
            original_data = b"original-envelope"
            with open(filename, "wb") as handle:
                handle.write(original_data)

            label = label_file.LabelFile()
            with mock.patch.object(
                label_file.os,
                "replace",
                side_effect=OSError("simulated write failure"),
            ):
                with self.assertRaises(label_file.LabelFileError):
                    label.save(
                        filename=filename,
                        shapes=[],
                        image_path="image.jpg",
                        image_height=1,
                        image_width=1,
                    )

            with open(filename, "rb") as handle:
                self.assertEqual(handle.read(), original_data)
            leftovers = [
                name
                for name in os.listdir(directory)
                if name
                not in {
                    "annotation.xalenc",
                    "project.key",
                    "project.key.meta.json",
                }
            ]
            self.assertEqual(leftovers, [])
            self.assertIsNone(label.filename)


if __name__ == "__main__":
    unittest.main()
