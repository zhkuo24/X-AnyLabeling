import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SETTINGS_ROOT = tempfile.mkdtemp(prefix="xany-loop-radius-settings-")
os.environ["XDG_CONFIG_HOME"] = _SETTINGS_ROOT

try:
    from PyQt6 import QtGui, QtWidgets

    from anylabeling import config as anylabeling_config
    from anylabeling.config import get_config, set_work_directory
    from anylabeling.resources import resources  # noqa: F401
    from anylabeling.views.mainwindow import MainWindow

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False


FRAME_COUNT = 17


def _sha256(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_frame(path, index):
    width = 100 + index
    image = QtGui.QImage(width, 24, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(index * 12 % 255, 40, 90))
    if not image.save(str(path)):
        raise AssertionError(f"failed to write {path}")
    payload = {
        "version": "5.0.0",
        "flags": {},
        "checked": False,
        "shapes": [
            {
                "label": f"frame-{index:02d}",
                "points": [[30, 18], [8, 18], [8, 4], [30, 4]],
                "group_id": None,
                "description": "",
                "shape_type": "rectangle",
                "flags": {},
                "mask": None,
            }
        ],
        "imagePath": Path(path).name,
        "imageData": None,
        "imageHeight": 24,
        "imageWidth": width,
        "description": f"note-{index:02d}",
    }
    Path(path).with_suffix(".json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


@unittest.skipUnless(PYQT_AVAILABLE, "PyQt6 is required for loop radius tests")
class TestLoopRadiusPreview(unittest.TestCase):
    """Changing the loop radius keeps the preview frame when it still fits.

    The radius control rebuilds the inclusive window around the anchor.
    The frame on screen has to stay that same source frame when the new
    window contains it. A frame that falls outside returns to the anchor.
    Either way the slider, the internal slot, and the next tick agree.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xany-loop-radius-")
        self.windows = []
        self.app = QtWidgets.QApplication.instance()
        if self.app is None:
            self.app = QtWidgets.QApplication([])
        self.app.setQuitOnLastWindowClosed(False)

    def tearDown(self):
        for window in self.windows:
            view = window.labeling_widget.view
            if view.playback_timer.isActive() or view._playback_mode:
                view._stop_playback()
            window.hide()
            window.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def _open_sequence(self, count=FRAME_COUNT):
        root = Path(self.temp_dir.name) / f"case-{len(self.windows)}"
        frame_dir = root / "frames"
        frame_dir.mkdir(parents=True)
        frames = []
        for index in range(count):
            path = frame_dir / f"{index:02d}.png"
            _write_frame(path, index)
            frames.append(path)
        set_work_directory(str(root / "settings"))
        anylabeling_config.current_config_file = "{}"
        config = get_config("{}")
        config["auto_save"] = True
        config["keep_prev"] = False
        config["exif_scan_enabled"] = False
        window = MainWindow(self.app, config=config, filename=str(frame_dir))
        window.resize(1000, 720)
        window.show()
        self.app.processEvents()
        self.windows.append(window)
        view = window.labeling_widget.view
        self.assertEqual(len(view.image_list), count)
        return view, frames

    def _snapshot(self, frames):
        return {
            path.name: _sha256(path.with_suffix(".json")) for path in frames
        }

    def _show_frame(self, view, frames, index):
        view.file_list_widget.setCurrentRow(index)
        self.app.processEvents()
        self.assertEqual(Path(view.filename), frames[index])

    def _start_loop(self, view, radius):
        view.playback_control.loop_radius_spinbox.setValue(radius)
        self.assertEqual(view._playback_loop_radius, radius)
        view.playback_control.loop_button.click()
        self.app.processEvents()
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertTrue(view.playback_timer.isActive())

    def _advance_to(self, view, frame_index):
        for _ in range(FRAME_COUNT + 2):
            if view._playback_display_index == frame_index:
                return
            view._on_playback_tick()
        self.fail(f"loop did not preview frame {frame_index}")

    def _set_radius(self, view, radius):
        view.playback_control.loop_radius_spinbox.setValue(radius)
        self.assertEqual(view._playback_loop_radius, radius)

    def _assert_preview(self, view, frames, anchor, preview):
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertTrue(view.playback_timer.isActive())
        self.assertEqual(view._playback_anchor_index, anchor)
        self.assertEqual(view._playback_display_index, preview)
        self.assertEqual(view.canvas.pixmap.width(), 100 + preview)
        self.assertEqual(view.image.width(), 100 + anchor)
        self.assertEqual(Path(view.filename), frames[anchor])
        self.assertEqual(Path(view.image_path), frames[anchor])
        self.assertEqual(
            [item.shape().label for item in view.label_list],
            [f"frame-{anchor:02d}"],
        )
        self.assertIn(preview, view._playback_loop_indices)
        self.assertEqual(
            view._playback_loop_indices[view._playback_loop_current_index],
            preview,
        )
        self.assertEqual(view.playback_control.current_frame, preview)
        self.assertEqual(view.playback_control.progress_slider.value(), preview)

    def _assert_anchor_shape_unchanged(self, view, shape, points):
        self.assertIs(view.canvas.shapes[0], shape)
        self.assertEqual(
            [(point.x(), point.y()) for point in shape.points],
            points,
        )

    def test_shrinking_keeps_a_preview_that_remains_inside(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, 5)
        self._advance_to(view, 12)
        shape = view.canvas.shapes[0]
        points = [(point.x(), point.y()) for point in shape.points]

        self._set_radius(view, 3)

        self._assert_preview(view, frames, 10, 12)
        self.assertEqual(view._playback_loop_indices, list(range(7, 14)))
        self._assert_anchor_shape_unchanged(view, shape, points)
        view._on_playback_tick()
        self._assert_preview(view, frames, 10, 13)
        self._assert_anchor_shape_unchanged(view, shape, points)
        self.assertEqual(self._snapshot(frames), before)

    def test_shrinking_returns_to_the_anchor_when_preview_falls_outside(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, 5)
        self._advance_to(view, 15)
        shape = view.canvas.shapes[0]
        points = [(point.x(), point.y()) for point in shape.points]

        self._set_radius(view, 1)

        self._assert_preview(view, frames, 10, 10)
        self.assertEqual(view._playback_loop_indices, [9, 10, 11])
        self._assert_anchor_shape_unchanged(view, shape, points)
        view._on_playback_tick()
        self._assert_preview(view, frames, 10, 11)
        self.assertEqual(self._snapshot(frames), before)

    def test_expanding_keeps_the_current_preview(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, 2)
        self._advance_to(view, 12)

        self._set_radius(view, 6)

        self._assert_preview(view, frames, 10, 12)
        self.assertEqual(view._playback_loop_indices, list(range(4, 17)))
        view._on_playback_tick()
        self._assert_preview(view, frames, 10, 13)
        self._set_radius(view, 999)
        self._assert_preview(view, frames, 10, 13)
        self.assertEqual(
            view._playback_loop_indices, list(range(FRAME_COUNT))
        )
        view._on_playback_tick()
        self._assert_preview(view, frames, 10, 14)
        self.assertEqual(self._snapshot(frames), before)

    def test_radius_at_sequence_ends_clamps_and_keeps_identity(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 0)
        self._start_loop(view, 3)
        self._advance_to(view, 3)
        self._set_radius(view, 1)
        self._assert_preview(view, frames, 0, 0)
        self.assertEqual(view._playback_loop_indices, [0, 1])
        view._on_playback_tick()
        self._assert_preview(view, frames, 0, 1)
        self._set_radius(view, 4)
        self._assert_preview(view, frames, 0, 1)
        self.assertEqual(view._playback_loop_indices, list(range(0, 5)))
        view._stop_playback()

        self._show_frame(view, frames, FRAME_COUNT - 1)
        self._start_loop(view, 3)
        self._advance_to(view, FRAME_COUNT - 4)
        self._set_radius(view, 1)
        self._assert_preview(view, frames, FRAME_COUNT - 1, FRAME_COUNT - 1)
        self.assertEqual(
            view._playback_loop_indices, [FRAME_COUNT - 2, FRAME_COUNT - 1]
        )
        view._on_playback_tick()
        self._assert_preview(view, frames, FRAME_COUNT - 1, FRAME_COUNT - 2)
        self.assertEqual(self._snapshot(frames), before)

    def test_minimum_window_stays_on_the_anchor(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, 5)
        self._advance_to(view, 15)
        shape = view.canvas.shapes[0]

        self._set_radius(view, 0)

        self._assert_preview(view, frames, 10, 10)
        self.assertEqual(view._playback_loop_indices, [10])
        self.assertIs(view.canvas.shapes[0], shape)
        view._on_playback_tick()
        self._assert_preview(view, frames, 10, 10)
        self.assertIs(view.canvas.shapes[0], shape)
        self.assertEqual(self._snapshot(frames), before)

    def test_radius_change_outside_a_loop_does_not_move(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 4)
        self._set_radius(view, 1)
        self.assertIsNone(view._playback_mode)
        self.assertFalse(view.playback_timer.isActive())
        self.assertEqual(Path(view.filename), frames[4])
        self.assertEqual(view.image.width(), 104)
        self.assertEqual(
            [item.shape().label for item in view.label_list],
            ["frame-04"],
        )

        view.playback_control.play_button.click()
        self.app.processEvents()
        self.assertEqual(view._playback_mode, view.PLAYBACK_SEQUENCE)
        view._on_playback_tick()
        view._on_playback_tick()
        self.assertEqual(Path(view.filename), frames[6])
        shown = view._playback_display_index
        self._set_radius(view, 8)
        self.assertEqual(view._playback_mode, view.PLAYBACK_SEQUENCE)
        self.assertEqual(view._playback_display_index, shown)
        self.assertEqual(Path(view.filename), frames[6])
        self.assertEqual(
            [item.shape().label for item in view.label_list],
            ["frame-06"],
        )
        view._stop_playback()
        self.assertEqual(Path(view.filename), frames[6])
        self._set_radius(view, 0)
        self.assertIsNone(view._playback_mode)
        self.assertEqual(Path(view.filename), frames[6])
        self.assertEqual(self._snapshot(frames), before)

    def test_single_frame_sequence_ignores_radius_changes(self):
        view, frames = self._open_sequence(count=1)
        before = self._snapshot(frames)
        self._set_radius(view, 0)
        self._set_radius(view, 5)
        view.playback_control.loop_button.click()
        self.app.processEvents()
        self.assertIsNone(view._playback_mode)
        self.assertFalse(view.playback_timer.isActive())
        self.assertEqual(Path(view.filename), frames[0])
        self.assertEqual(
            [item.shape().label for item in view.label_list],
            ["frame-00"],
        )
        self.assertEqual(self._snapshot(frames), before)
