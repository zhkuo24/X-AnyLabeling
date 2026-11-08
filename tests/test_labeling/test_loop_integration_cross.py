import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SETTINGS_ROOT = tempfile.mkdtemp(prefix="xany-loop-cross-settings-")
os.environ["XDG_CONFIG_HOME"] = _SETTINGS_ROOT

try:
    from PyQt6 import QtCore, QtGui, QtWidgets

    from anylabeling import config as anylabeling_config
    from anylabeling.config import get_config, set_work_directory
    from anylabeling.resources import resources  # noqa: F401
    from anylabeling.views.labeling.shape import Shape
    from anylabeling.views.mainwindow import MainWindow

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False


FRAME_COUNT = 17


def _sha256(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_frame(path, index, shapes):
    width = 40 + index
    image = QtGui.QImage(width, 24, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(index * 10 % 255, 40, 90))
    if not image.save(str(path)):
        raise AssertionError(f"failed to write {path}")
    payload = {
        "version": "5.0.0",
        "flags": {},
        "checked": False,
        "shapes": shapes,
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


def _rectangle(label):
    return {
        "label": label,
        "points": [[30, 18], [8, 18], [8, 4], [30, 4]],
        "group_id": None,
        "description": "",
        "shape_type": "rectangle",
        "flags": {},
        "mask": None,
    }


@unittest.skipUnless(PYQT_AVAILABLE, "PyQt6 is required for loop cross tests")
class TestLoopIntegrationCross(unittest.TestCase):
    """Radius changes keep the other loop guarantees.

    The preview slot, an open shape, a failed save, and navigation all
    meet in one running loop. Changing the radius must not drop any of
    them or rewrite a neighbour frame.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xany-loop-cross-")
        self.windows = []
        self.app = QtWidgets.QApplication.instance()
        if self.app is None:
            self.app = QtWidgets.QApplication([])
        self.app.setQuitOnLastWindowClosed(False)

    def tearDown(self):
        for root, dirs, files in os.walk(self.temp_dir.name):
            os.chmod(root, 0o755)
            for name in files:
                try:
                    os.chmod(os.path.join(root, name), 0o644)
                except OSError:
                    pass
        for window in self.windows:
            view = window.labeling_widget.view
            if view.playback_timer.isActive() or view._playback_mode:
                view._stop_playback()
            window.hide()
            window.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def _open_sequence(self, auto_save=True, keep_prev=False, empty_indexes=()):
        root = Path(self.temp_dir.name) / f"case-{len(self.windows)}"
        frame_dir = root / "frames"
        frame_dir.mkdir(parents=True)
        frames = []
        for index in range(FRAME_COUNT):
            path = frame_dir / f"{index:02d}.png"
            shapes = (
                []
                if index in empty_indexes
                else [_rectangle(f"frame-{index:02d}")]
            )
            _write_frame(path, index, shapes)
            frames.append(path)
        set_work_directory(str(root / "settings"))
        anylabeling_config.current_config_file = "{}"
        config = get_config("{}")
        config["auto_save"] = auto_save
        config["keep_prev"] = keep_prev
        config["exif_scan_enabled"] = False
        window = MainWindow(self.app, config=config, filename=str(frame_dir))
        window.resize(1000, 720)
        window.show()
        self.app.processEvents()
        self.windows.append(window)
        view = window.labeling_widget.view
        self.assertEqual(len(view.image_list), FRAME_COUNT)
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

    def _labels(self, view):
        return [item.shape().label for item in view.label_list]

    def _open_drawing(self):
        shape = Shape(shape_type="polygon", flags={})
        shape.points = [QtCore.QPointF(4, 4), QtCore.QPointF(18, 10)]
        return shape

    def _assert_stopped_on(self, view, frames, index):
        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)
        self.assertEqual(Path(view.filename), frames[index])
        self.assertEqual(Path(view.image_path), frames[index])
        self.assertEqual(view.image.width(), 40 + index)
        self.assertEqual(view.canvas.pixmap.width(), 40 + index)
        self.assertEqual(view.playback_control.current_frame, index)

    @contextmanager
    def _answer_modal(self, button, delay_ms=100):
        state = {"seen": 0, "clicked": 0, "since": None}
        timer = QtCore.QTimer()
        timer.setInterval(20)
        safety = QtCore.QTimer()
        safety.setSingleShot(True)

        def poll():
            box = QtWidgets.QApplication.activeModalWidget()
            if not isinstance(box, QtWidgets.QMessageBox):
                state["since"] = None
                return
            now = time.monotonic()
            if state["since"] is None:
                state["since"] = now
                state["seen"] += 1
            if (now - state["since"]) * 1000 < delay_ms:
                return
            widget = box.button(button)
            if widget is not None:
                state["clicked"] += 1
                state["since"] = None
                widget.click()

        def abort():
            box = QtWidgets.QApplication.activeModalWidget()
            if isinstance(box, QtWidgets.QMessageBox):
                box.reject()

        timer.timeout.connect(poll)
        safety.timeout.connect(abort)
        timer.start()
        safety.start(2000)
        try:
            yield state
        finally:
            timer.stop()
            safety.stop()

    def _close_save_error(self, view, action):
        state = {"closed": 0, "timeout": False, "stop": False}

        def pump(remaining=200):
            if state["stop"]:
                return
            box = QtWidgets.QApplication.activeModalWidget()
            if isinstance(box, QtWidgets.QMessageBox):
                box.done(QtWidgets.QMessageBox.StandardButton.Ok)
                state["closed"] += 1
                return
            if remaining <= 0:
                state["timeout"] = True
                return
            QtCore.QTimer.singleShot(15, lambda: pump(remaining - 1))

        QtCore.QTimer.singleShot(0, pump)
        try:
            action()
        finally:
            state["stop"] = True
        self.app.processEvents()
        self.assertFalse(state["timeout"])
        self.assertGreaterEqual(state["closed"], 1)

    def test_radius_change_keeps_open_geometry_and_dirty(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, 5)
        self._advance_to(view, 12)
        drawing = self._open_drawing()
        view.canvas.current = drawing
        view.dirty = True
        anchor_shape = view.canvas.shapes[0]

        self._set_radius(view, 3)

        self.assertEqual(view._playback_display_index, 12)
        self.assertEqual(view.canvas.pixmap.width(), 52)
        self.assertEqual(
            view._playback_loop_indices[view._playback_loop_current_index],
            12,
        )
        self.assertIs(view.canvas.current, drawing)
        self.assertIs(view.canvas.shapes[0], anchor_shape)
        self.assertEqual(self._labels(view), ["frame-10"])
        self.assertTrue(view.dirty)
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertEqual(Path(view.filename), frames[10])
        self.assertEqual(self._snapshot(frames), before)

        self._set_radius(view, 5)
        self._advance_to(view, 15)
        self._set_radius(view, 1)

        self.assertEqual(view._playback_display_index, 10)
        self.assertEqual(view.canvas.pixmap.width(), 50)
        self.assertEqual(view.playback_control.current_frame, 10)
        self.assertEqual(view._playback_loop_indices, [9, 10, 11])
        self.assertIs(view.canvas.current, drawing)
        self.assertIs(view.canvas.shapes[0], anchor_shape)
        self.assertTrue(view.dirty)
        self.assertEqual(Path(view.filename), frames[10])
        self.assertEqual(view.image.width(), 50)
        view._on_playback_tick()
        self.assertEqual(view._playback_display_index, 11)
        self.assertIs(view.canvas.current, drawing)
        self.assertEqual(self._snapshot(frames), before)

    def test_navigation_after_radius_change_uses_the_preview(self):
        view, frames = self._open_sequence(
            keep_prev=True, empty_indexes={4}
        )
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, 5)
        self._advance_to(view, 12)
        self._set_radius(view, 3)
        self.assertEqual(view._playback_display_index, 12)

        view.playback_control.next_button.click()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 13)
        self.assertEqual(self._labels(view), ["frame-13"])

        self._show_frame(view, frames, 10)
        self._start_loop(view, 5)
        self._advance_to(view, 12)
        self._set_radius(view, 3)
        view.actions.open_prev_image.trigger()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 11)
        self.assertEqual(self._labels(view), ["frame-11"])

        self._show_frame(view, frames, 10)
        self._start_loop(view, 5)
        self._advance_to(view, 12)
        self._set_radius(view, 3)
        view.playback_control.seek_requested.emit(4)
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 4)
        self.assertEqual(self._labels(view), [])
        sought = json.loads(
            frames[4].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(sought["shapes"], [])
        self.assertEqual(sought["imagePath"], frames[4].name)
        self.assertEqual(self._snapshot(frames), before)

    def test_cancel_after_radius_change_keeps_the_edit(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, 5)
        self._advance_to(view, 12)
        self._set_radius(view, 3)
        view.canvas.shapes[0].label = "edited-anchor"
        view.set_dirty()
        ticks = {"count": 0}
        view.playback_timer.timeout.connect(
            lambda: ticks.__setitem__("count", ticks["count"] + 1)
        )
        cancel = QtWidgets.QMessageBox.StandardButton.Cancel

        ticks["count"] = 0
        with self._answer_modal(cancel, delay_ms=100) as state:
            view.actions.open_next_image.trigger()

        self.assertEqual(state["seen"], 1)
        self.assertEqual(state["clicked"], 1)
        self.assertGreater(ticks["count"], 0)
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertTrue(view.playback_timer.isActive())
        self.assertEqual(view._playback_display_index, 12)
        self.assertEqual(view._playback_anchor_index, 10)
        self.assertEqual(self._labels(view), ["edited-anchor"])
        self.assertTrue(view.dirty)
        self.assertEqual(Path(view.filename), frames[10])
        self.assertEqual(self._snapshot(frames), before)

    def test_save_failure_after_radius_change_keeps_shapes(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, 5)
        self._advance_to(view, 15)
        self._set_radius(view, 1)
        self.assertEqual(view._playback_display_index, 10)
        shape = view.canvas.shapes[0]
        shape.label = "unsaved-anchor"
        view.set_dirty()
        os.chmod(frames[0].parent, 0o555)

        self._close_save_error(view, lambda: view.toggle_loop_playback(False))

        self.assertIsNone(view._playback_mode)
        self.assertFalse(view.playback_timer.isActive())
        self.assertTrue(view.dirty)
        self.assertIs(view.canvas.shapes[0], shape)
        self.assertEqual(self._labels(view), ["unsaved-anchor"])
        self.assertEqual(Path(view.filename), frames[10])
        self.assertEqual(view.canvas.pixmap.width(), 50)
        self.assertEqual(self._snapshot(frames), before)
        anchor = json.loads(
            frames[10].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(anchor["shapes"][0]["label"], "frame-10")
        for index, path in enumerate(frames):
            saved = json.loads(
                path.with_suffix(".json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["imagePath"], path.name)
            self.assertEqual(
                saved["shapes"][0]["label"], f"frame-{index:02d}"
            )
