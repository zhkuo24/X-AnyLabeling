import json
import os
import tempfile
import unittest
from unittest import mock

import yaml

from anylabeling.views.labeling.label_converter import LabelConverter


class TestLabelConverterPoseConfig(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _write_pose_cfg(self, data):
        cfg_path = os.path.join(self.temp_dir, "pose.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        return cfg_path

    def test_missing_has_visible_defaults_to_true(self):
        cfg_path = self._write_pose_cfg(
            {"classes": {"person": ["nose", "left_eye"]}}
        )

        converter = LabelConverter(pose_cfg_file=cfg_path)

        self.assertTrue(converter.has_visible)
        self.assertEqual(converter.classes, ["person"])

    def test_explicit_has_visible_false_is_respected(self):
        cfg_path = self._write_pose_cfg(
            {
                "has_visible": False,
                "classes": {"person": ["nose", "left_eye"]},
            }
        )

        converter = LabelConverter(pose_cfg_file=cfg_path)

        self.assertFalse(converter.has_visible)

    def test_missing_classes_raises_value_error(self):
        cfg_path = self._write_pose_cfg({"has_visible": True})

        with self.assertRaises(ValueError):
            LabelConverter(pose_cfg_file=cfg_path)


class TestLabelConverterObbBounds(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.classes_file = os.path.join(self.temp_dir, "classes.txt")
        with open(self.classes_file, "w", encoding="utf-8") as f:
            f.write("plane\n")
        self.converter = LabelConverter(classes_file=self.classes_file)

    def tearDown(self):
        import shutil

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _write_label_file(self, points):
        label_file = os.path.join(self.temp_dir, "label.json")
        data = {
            "imagePath": "image.jpg",
            "imageWidth": 100,
            "imageHeight": 50,
            "shapes": [
                {
                    "label": "plane",
                    "shape_type": "rotation",
                    "points": points,
                }
            ],
        }
        with open(label_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return label_file

    def test_dota_skips_rotation_shape_with_any_out_of_bounds_point(self):
        label_file = self._write_label_file(
            [[-1, 10], [20, 10], [20, 20], [10, 20]]
        )
        output_file = os.path.join(self.temp_dir, "label.txt")

        self.converter.custom_to_dota(label_file, output_file)

        with open(output_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "")

    def test_yolo_obb_skips_rotation_shape_with_any_out_of_bounds_point(self):
        label_file = self._write_label_file(
            [[-1, 10], [20, 10], [20, 20], [10, 20]]
        )
        output_file = os.path.join(self.temp_dir, "label.txt")

        self.converter.custom_to_yolo(label_file, output_file, "obb")

        with open(output_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "")

    def test_yolo_and_coco_read_encrypted_labels(self):
        from anylabeling.label_crypto import (
            clear_key_cache,
            configure_key_file,
            encrypt_bytes,
            generate_key_file,
        )

        self.addCleanup(clear_key_cache)
        clear_key_cache()
        image_path = os.path.join(self.temp_dir, "frame.png")
        with open(image_path, "wb") as handle:
            handle.write(b"")
        label = {
            "imagePath": "frame.png",
            "imageWidth": 100,
            "imageHeight": 50,
            "shapes": [
                {
                    "label": "plane",
                    "shape_type": "rectangle",
                    "points": [[10, 10], [40, 10], [40, 30], [10, 30]],
                }
            ],
        }
        plain_path = os.path.join(self.temp_dir, "frame.json")
        with open(plain_path, "w", encoding="utf-8") as handle:
            json.dump(label, handle)
        plain_out = os.path.join(self.temp_dir, "plain.txt")
        self.converter.custom_to_yolo(plain_path, plain_out, "hbb")

        material = generate_key_file(
            os.path.join(self.temp_dir, "project.key")
        )
        configure_key_file(material.path)
        encrypted_path = os.path.join(self.temp_dir, "frame.xalenc")
        with open(plain_path, "rb") as handle:
            encrypted = encrypt_bytes(handle.read(), material)
        with open(encrypted_path, "wb") as handle:
            handle.write(encrypted)
        os.remove(plain_path)

        encrypted_out = os.path.join(self.temp_dir, "encrypted.txt")
        self.converter.custom_to_yolo(encrypted_path, encrypted_out, "hbb")
        with (
            open(plain_out, encoding="utf-8") as plain,
            open(encrypted_out, encoding="utf-8") as restored,
        ):
            exported = restored.read()
            self.assertEqual(plain.read(), exported)
            self.assertTrue(exported.startswith("0 "))

        resolved = self.converter.resolve_xlabel_path(
            image_path, self.temp_dir
        )
        self.assertEqual(
            os.path.abspath(resolved), os.path.abspath(encrypted_path)
        )
        coco_dir = os.path.join(self.temp_dir, "coco")
        os.makedirs(coco_dir)
        self.converter.custom_to_coco(
            [image_path], self.temp_dir, coco_dir, "rectangle"
        )
        with open(
            os.path.join(coco_dir, "coco_detection.json"), encoding="utf-8"
        ) as handle:
            coco = json.load(handle)
        self.assertEqual(len(coco["annotations"]), 1)
        self.assertEqual(coco["images"][0]["file_name"], "frame.png")


class TestLabelConverterVocValidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.converter = LabelConverter()
        self.input_file = os.path.join(self.temp_dir.name, "input.xml")
        self.output_file = os.path.join(self.temp_dir.name, "output.json")

    def _convert(self, objects, mode="rectangle"):
        xml = (
            "<annotation><filename>image.jpg</filename>"
            "<size><width>100</width><height>50</height></size>"
            f"{objects}</annotation>"
        )
        with open(self.input_file, "w", encoding="utf-8") as f:
            f.write(xml)
        self.converter.voc_to_custom(
            self.input_file, self.output_file, "image.jpg", mode
        )
        with open(self.output_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_missing_geometry_is_skipped(self):
        objects = (
            "<object><name>missing</name></object>"
            "<object><name>valid</name><bndbox>"
            "<xmin>1</xmin><ymin>2</ymin><xmax>3</xmax><ymax>4</ymax>"
            "</bndbox></object>"
        )

        with mock.patch(
            "anylabeling.views.labeling.label_converter.logger.warning"
        ) as warning:
            data = self._convert(objects)

        self.assertEqual(
            [shape["label"] for shape in data["shapes"]], ["valid"]
        )
        warning.assert_called_once()
        self.assertIn("VOC object 1", warning.call_args.args[0])
        self.assertIn(self.input_file, warning.call_args.args[0])

    def test_incomplete_geometry_is_skipped(self):
        objects = (
            "<object><name>incomplete</name><bndbox>"
            "<xmin>1</xmin><ymin>2</ymin><xmax>3</xmax>"
            "</bndbox></object>"
        )

        with mock.patch(
            "anylabeling.views.labeling.label_converter.logger.warning"
        ) as warning:
            data = self._convert(objects)

        self.assertEqual(data["shapes"], [])
        warning.assert_called_once()
        self.assertIn("bndbox/ymax", warning.call_args.args[0])
