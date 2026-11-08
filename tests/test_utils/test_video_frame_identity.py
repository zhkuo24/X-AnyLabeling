import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

from anylabeling.views.labeling.utils import video as video_module
from anylabeling.views.labeling.utils.video import (
    FRAME_SOURCE_MANIFEST,
    ExtractionCancelled,
    VideoFrameIdentityError,
    extract_sampled_frames,
    install_video_extraction,
    open_video_file,
    read_frame_source_manifest,
)

# Distinct BGR colors. JPEG rounding stays well inside the tolerance below.
COLORS = (
    (0, 0, 220),
    (0, 220, 0),
    (220, 0, 0),
    (0, 220, 220),
    (220, 0, 220),
    (220, 220, 0),
    (30, 30, 220),
    (30, 220, 30),
    (220, 30, 30),
    (80, 160, 40),
)


def _snapshot(directory):
    digest = hashlib.sha256()
    root = Path(directory)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_clip(path, fps=10.0):
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (32, 24)
    )
    if not writer.isOpened():
        raise AssertionError(f"OpenCV could not write {path}")
    try:
        for color in COLORS:
            frame = np.zeros((24, 32, 3), dtype=np.uint8)
            frame[:, :] = color
            writer.write(frame)
    finally:
        writer.release()


def _mean_bgr(path):
    image = cv2.imread(str(path))
    if image is None:
        raise AssertionError(f"unreadable frame: {path}")
    return image.mean(axis=(0, 1))


def _close_to(actual, expected, tolerance=15):
    return (
        np.max(np.abs(np.asarray(actual) - np.asarray(expected))) <= tolerance
    )


class TestVideoFrameIdentity(unittest.TestCase):

    def setUp(self):
        self.app = QApplication.instance()
        if self.app is None:
            self.app = QApplication([])
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="xany-video-frames-"
        )
        self.root = Path(self.temp_dir.name)
        self.video = self.root / "clip.avi"
        _write_clip(self.video)
        self.ffmpeg = shutil.which("ffmpeg")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _extract(self, destination, *, extractor, interval, prefix="frame_"):
        destination.mkdir(parents=True, exist_ok=True)
        return extract_sampled_frames(
            str(self.video),
            str(destination),
            extractor=extractor,
            interval=interval,
            prefix=prefix,
            seq_len=5,
            fps=10.0,
            ffmpeg_path=self.ffmpeg,
        )

    def test_interval_change_does_not_attach_old_json_to_new_pixels(self):
        output = self.root / "frames"
        self._extract(output, extractor="opencv", interval=1)
        marker = '{"shapes":[{"label":"source-frame-1"}]}'
        (output / "frame_00001.json").write_text(marker, encoding="utf-8")
        self.assertTrue(
            _close_to(_mean_bgr(output / "frame_00001.jpg"), COLORS[1])
        )
        before = _snapshot(output)

        resampled = self.root / "interval-3"
        self._extract(resampled, extractor="opencv", interval=3)
        self.assertTrue(
            _close_to(_mean_bgr(resampled / "frame_00001.jpg"), COLORS[3])
        )
        self.assertFalse(
            _close_to(_mean_bgr(resampled / "frame_00001.jpg"), COLORS[1])
        )

        with self.assertRaises(VideoFrameIdentityError):
            install_video_extraction(str(resampled), str(output))

        self.assertEqual(_snapshot(output), before)
        self.assertEqual(
            (output / "frame_00001.json").read_text(encoding="utf-8"), marker
        )
        self.assertTrue(resampled.is_dir())
        self.assertTrue(
            _close_to(_mean_bgr(output / "frame_00001.jpg"), COLORS[1])
        )

    def test_same_rule_reimport_keeps_annotations_on_the_same_source_frame(
        self,
    ):
        output = self.root / "frames"
        self._extract(output, extractor="opencv", interval=1)
        marker = '{"shapes":[{"label":"source-frame-1"}]}'
        (output / "frame_00001.json").write_text(marker, encoding="utf-8")
        (output / "notes.txt").write_text("reviewed", encoding="utf-8")

        again = self.root / "again"
        self._extract(again, extractor="opencv", interval=1)
        self.assertTrue(
            _close_to(_mean_bgr(again / "frame_00001.jpg"), COLORS[1])
        )
        install_video_extraction(str(again), str(output))

        self.assertEqual(
            (output / "frame_00001.json").read_text(encoding="utf-8"), marker
        )
        self.assertEqual(
            (output / "notes.txt").read_text(encoding="utf-8"), "reviewed"
        )
        self.assertTrue(
            _close_to(_mean_bgr(output / "frame_00001.jpg"), COLORS[1])
        )
        self.assertFalse(again.exists())
        manifest = read_frame_source_manifest(str(output))
        self.assertEqual(manifest["extractor"], "opencv")
        self.assertEqual(manifest["interval"], 1)
        self.assertEqual(
            manifest["frame_selection"], "frame_index_mod_interval"
        )

    def test_legacy_directory_without_manifest_is_left_unchanged(self):
        output = self.root / "legacy"
        self._extract(output, extractor="opencv", interval=1)
        (output / FRAME_SOURCE_MANIFEST).unlink()
        (output / "frame_00000.json").write_text(
            '{"shapes":[{"label":"legacy"}]}', encoding="utf-8"
        )
        before = _snapshot(output)

        again = self.root / "again"
        self._extract(again, extractor="opencv", interval=1)
        with self.assertRaises(VideoFrameIdentityError):
            install_video_extraction(str(again), str(output))

        self.assertEqual(_snapshot(output), before)
        self.assertFalse((output / FRAME_SOURCE_MANIFEST).exists())

    def test_ffmpeg_and_opencv_sampling_are_not_treated_as_the_same_frames(
        self,
    ):
        if self.ffmpeg is None:
            self.skipTest("ffmpeg is not installed")
        opencv_dir = self.root / "opencv"
        ffmpeg_dir = self.root / "ffmpeg"
        self._extract(opencv_dir, extractor="opencv", interval=3)
        self._extract(ffmpeg_dir, extractor="ffmpeg", interval=3)
        opencv_first = _mean_bgr(opencv_dir / "frame_00000.jpg")
        ffmpeg_first = _mean_bgr(ffmpeg_dir / "frame_00000.jpg")
        self.assertGreater(np.linalg.norm(opencv_first - ffmpeg_first), 50)
        self.assertTrue(_close_to(opencv_first, COLORS[0]))
        self.assertFalse(_close_to(ffmpeg_first, COLORS[0]))

        marker = '{"shapes":[{"label":"opencv-frame-0"}]}'
        (opencv_dir / "frame_00000.json").write_text(marker, encoding="utf-8")
        before = _snapshot(opencv_dir)
        opencv_manifest = read_frame_source_manifest(str(opencv_dir))
        ffmpeg_manifest = read_frame_source_manifest(str(ffmpeg_dir))
        self.assertNotEqual(
            opencv_manifest["frame_selection"],
            ffmpeg_manifest["frame_selection"],
        )

        with self.assertRaises(VideoFrameIdentityError):
            install_video_extraction(str(ffmpeg_dir), str(opencv_dir))

        self.assertEqual(_snapshot(opencv_dir), before)
        self.assertEqual(
            (opencv_dir / "frame_00000.json").read_text(encoding="utf-8"),
            marker,
        )

    def test_cancel_and_unreadable_video_do_not_change_existing_annotations(
        self,
    ):
        output = self.root / "frames"
        self._extract(output, extractor="opencv", interval=1)
        (output / "frame_00000.json").write_text(
            '{"shapes":[{"label":"keep"}]}', encoding="utf-8"
        )
        before = _snapshot(output)

        cancelled = self.root / "cancelled"
        cancelled.mkdir()
        with self.assertRaises(ExtractionCancelled):
            extract_sampled_frames(
                str(self.video),
                str(cancelled),
                extractor="opencv",
                interval=1,
                prefix="frame_",
                seq_len=5,
                fps=10.0,
                is_cancelled=lambda: True,
            )
        self.assertFalse((cancelled / FRAME_SOURCE_MANIFEST).exists())
        self.assertEqual(_snapshot(output), before)

        failed = self.root / "failed"
        failed.mkdir()
        not_a_video = self.root / "notes.txt"
        not_a_video.write_text("not a video", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            extract_sampled_frames(
                str(not_a_video),
                str(failed),
                extractor="opencv",
                interval=1,
                prefix="frame_",
                seq_len=5,
                fps=10.0,
            )
        self.assertEqual(_snapshot(output), before)
        self.assertFalse((failed / FRAME_SOURCE_MANIFEST).exists())

    def test_directory_without_annotations_can_be_replaced(self):
        output = self.root / "frames"
        self._extract(output, extractor="opencv", interval=1)
        (output / FRAME_SOURCE_MANIFEST).unlink()
        self.assertTrue(
            _close_to(_mean_bgr(output / "frame_00001.jpg"), COLORS[1])
        )

        resampled = self.root / "interval-3"
        self._extract(resampled, extractor="opencv", interval=3)
        install_video_extraction(str(resampled), str(output))

        self.assertTrue(
            _close_to(_mean_bgr(output / "frame_00001.jpg"), COLORS[3])
        )
        annotation_json = [
            path.name
            for path in output.glob("*.json")
            if path.name != FRAME_SOURCE_MANIFEST
        ]
        self.assertEqual(annotation_json, [])
        self.assertFalse(resampled.exists())
        manifest = read_frame_source_manifest(str(output))
        self.assertEqual(manifest["interval"], 3)
        self.assertEqual(manifest["extractor"], "opencv")

    def test_stale_manifest_does_not_describe_replaced_unlabeled_frames(self):
        output = self.root / "frames"
        self._extract(output, extractor="opencv", interval=1)
        self.assertEqual(
            read_frame_source_manifest(str(output))["interval"], 1
        )

        resampled = self.root / "interval-3"
        self._extract(resampled, extractor="opencv", interval=3)
        install_video_extraction(str(resampled), str(output))

        self.assertEqual(
            read_frame_source_manifest(str(output))["interval"], 3
        )
        self.assertTrue(
            _close_to(_mean_bgr(output / "frame_00001.jpg"), COLORS[3])
        )

    def test_corrupt_manifest_is_not_proof(self):
        output = self.root / "frames"
        self._extract(output, extractor="opencv", interval=1)
        (output / FRAME_SOURCE_MANIFEST).write_text(
            "not-json", encoding="utf-8"
        )
        (output / "frame_00000.json").write_text(
            '{"shapes":[{"label":"keep"}]}', encoding="utf-8"
        )
        before = _snapshot(output)
        again = self.root / "again"
        self._extract(again, extractor="opencv", interval=1)
        with self.assertRaises(VideoFrameIdentityError):
            install_video_extraction(str(again), str(output))
        self.assertEqual(_snapshot(output), before)

    def test_open_video_file_keeps_annotations_when_sampling_changes(self):
        imported = []
        warnings = []
        intervals = iter((1, 1, 3))

        class FakeDialog:
            def __init__(self, *_args, **_kwargs):
                self.interval = next(intervals)

            def exec(self):
                return True

            def get_values(self):
                return (self.interval, "frame_", 5)

        class FakeMessageBox:
            Icon = QMessageBox.Icon
            StandardButton = QMessageBox.StandardButton

            def setIcon(self, *_args):
                return None

            def setWindowTitle(self, *_args):
                return None

            def setText(self, *_args):
                return None

            def setInformativeText(self, *_args):
                return None

            def setStandardButtons(self, *_args):
                return None

            def setDefaultButton(self, *_args):
                return None

            def setStyleSheet(self, *_args):
                return None

            def exec(self):
                return QMessageBox.StandardButton.Ok

            @staticmethod
            def warning(_parent, title, text):
                warnings.append((title, text))
                return QMessageBox.StandardButton.Ok

        class Host(QWidget):
            def __init__(self):
                super().__init__()
                self.imported = imported

            def may_continue(self):
                return True

            def import_image_folder(self, path):
                self.imported.append(path)

        host = Host()
        output = self.root / "clip"
        with (
            mock.patch.object(
                video_module, "FrameExtractionDialog", FakeDialog
            ),
            mock.patch.object(
                video_module.QFileDialog,
                "getOpenFileName",
                return_value=(str(self.video), ""),
            ),
            mock.patch.object(video_module, "QMessageBox", FakeMessageBox),
        ):
            open_video_file(host)
            marker = '{"shapes":[{"label":"source-frame-1"}]}'
            (output / "frame_00001.json").write_text(marker, encoding="utf-8")
            first_color = _mean_bgr(output / "frame_00001.jpg")
            open_video_file(host)
            self.assertEqual(
                (output / "frame_00001.json").read_text(encoding="utf-8"),
                marker,
            )
            self.assertTrue(
                _close_to(
                    _mean_bgr(output / "frame_00001.jpg"), first_color
                )
            )
            before = _snapshot(output)

            open_video_file(host)

        self.assertEqual(len(imported), 2)
        self.assertEqual(len(warnings), 1)
        self.assertIn("left unchanged", warnings[0][1])
        self.assertEqual(_snapshot(output), before)
        self.assertEqual(
            (output / "frame_00001.json").read_text(encoding="utf-8"), marker
        )
        manifest = read_frame_source_manifest(str(output))
        self.assertEqual(manifest["interval"], 1)
        self.assertEqual(
            manifest["extractor"], "ffmpeg" if self.ffmpeg else "opencv"
        )
