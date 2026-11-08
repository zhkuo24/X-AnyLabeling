import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from anylabeling.views.common.converter import run_conversion
from anylabeling.views.labeling.label_widget import LabelingWidget
from anylabeling.views.labeling.utils.shape import _apply_shape_conversion
from anylabeling.views.labeling.utils.video import (
    FRAME_SOURCE_MANIFEST,
    write_frame_source_manifest,
)


def _label_widget(frames, image_name):
    widget = type("Widget", (), {})()
    widget.image_list = [str(frames / image_name)]
    widget.filename = widget.image_list[0]
    widget.output_dir = str(frames)
    return widget


class FrameSourceManifestExportTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="xany-manifest-export-"))
        self.frames = self.root / "frames"
        self.frames.mkdir()
        image = np.zeros((24, 32, 3), dtype=np.uint8)
        image[:] = (0, 220, 0)
        cv2.imwrite(str(self.frames / "frame_00000.jpg"), image)
        self.label = {
            "version": "5.0.0",
            "flags": {},
            "shapes": [
                {
                    "label": "person",
                    "points": [[1, 1], [10, 1], [10, 10], [1, 10]],
                    "group_id": 1,
                    "shape_type": "rectangle",
                    "flags": {},
                    "difficult": False,
                }
            ],
            "imagePath": "frame_00000.jpg",
            "imageData": None,
            "imageHeight": 24,
            "imageWidth": 32,
        }
        label_path = self.frames / "frame_00000.json"
        label_path.write_text(json.dumps(self.label), encoding="utf-8")
        source = self.root / "clip.bin"
        source.write_bytes(b"synthetic-source")
        write_frame_source_manifest(
            str(self.frames),
            source_path=str(source),
            extractor="opencv",
            interval=1,
            prefix="frame_",
            seq_len=5,
            fps=10.0,
        )
        self.classes = self.root / "classes.txt"
        self.classes.write_text("person\n", encoding="utf-8")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_written_manifest_is_not_a_label_json(self):
        self.assertFalse(FRAME_SOURCE_MANIFEST.endswith("json"))
        self.assertTrue((self.frames / FRAME_SOURCE_MANIFEST).is_file())
        names = {path.name for path in self.frames.iterdir()}
        self.assertIn(FRAME_SOURCE_MANIFEST, names)
        self.assertNotIn(".xany-frame-source.json", names)

    def test_shape_conversion_skips_manifest_and_keeps_its_bytes(self):
        widget = _label_widget(self.frames, "frame_00000.jpg")
        listed = LabelingWidget.get_label_file_list(widget)
        listed_names = [Path(path).name for path in listed]
        self.assertEqual(listed_names, ["frame_00000.json"])
        before = (self.frames / FRAME_SOURCE_MANIFEST).read_bytes()

        for path in listed:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            _apply_shape_conversion(data, "rectangle_to_polygon", {})
            Path(path).write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        converted = json.loads(
            (self.frames / "frame_00000.json").read_text(encoding="utf-8")
        )
        self.assertEqual(converted["shapes"][0]["shape_type"], "polygon")
        self.assertEqual(
            (self.frames / FRAME_SOURCE_MANIFEST).read_bytes(), before
        )

    def test_cli_mot_and_yolo_export_the_annotation_only(self):
        mot_output = self.root / "mot"
        run_conversion(
            "xlabel2mot",
            labels=str(self.frames),
            output=str(mot_output),
            classes_file=str(self.classes),
        )
        gt = (mot_output / "gt.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(gt, ["0,1,1,1,9,9,1,0,1"])
        seqinfo = (mot_output / "seqinfo.ini").read_text(encoding="utf-8")
        self.assertIn("seqlength = 1", seqinfo)

        mots_output = self.root / "mots"
        run_conversion(
            "xlabel2mots",
            labels=str(self.frames),
            output=str(mots_output),
            classes_file=str(self.classes),
        )
        seqinfo = (mots_output / "seqinfo.ini").read_text(encoding="utf-8")
        self.assertIn("seqlength = 1", seqinfo)
        self.assertEqual(
            (mots_output / "custom_gt.txt").read_text(encoding="utf-8"), ""
        )

        yolo_output = self.root / "yolo"
        run_conversion(
            "xlabel2yolo",
            images=str(self.frames),
            labels=str(self.frames),
            output=str(yolo_output),
            classes_file=str(self.classes),
            mode="detect",
        )
        exported = sorted(path.name for path in yolo_output.iterdir())
        self.assertEqual(exported, ["frame_00000.txt"])
        yolo_row = (yolo_output / "frame_00000.txt").read_text(
            encoding="utf-8"
        )
        self.assertTrue(yolo_row.startswith("0 "))
