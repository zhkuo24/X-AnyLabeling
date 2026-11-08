import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SETTINGS_ROOT = tempfile.mkdtemp(prefix="xany-loop-step-settings-")
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
    height = 24
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
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
        "imageHeight": height,
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


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for loop step tests"
)
class TestLoopStepPreviewFrame(unittest.TestCase):
    """Next and previous during a loop move from the preview frame.

    Stopping the loop restores the anchor document first. The step that
    follows must still use the frame that was on screen, and must not
    write that anchor document onto the destination.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xany-loop-step-")
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
        self.assertEqual(Path(view.filename), frames[0])
        return view, frames

    def _snapshot(self, frames):
        return {
            path.name: _sha256(path.with_suffix(".json")) for path in frames
        }

    def _show_frame(self, view, frames, index):
        view.file_list_widget.setCurrentRow(index)
        self.app.processEvents()
        self.assertEqual(Path(view.filename), frames[index])
        self.assertEqual(view.fn_to_index[str(view.filename)], index)

    def _start_loop(self, view, radius):
        view.playback_control.loop_radius_spinbox.setValue(radius)
        self.app.processEvents()
        self.assertEqual(view._playback_loop_radius, radius)
        view.playback_control.loop_button.click()
        self.app.processEvents()
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertTrue(view.playback_timer.isActive())

    def _advance_loop_to(self, view, frame_index):
        for _ in range(FRAME_COUNT + 2):
            if view._playback_display_index == frame_index:
                self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
                return
            view._on_playback_tick()
        self.fail(f"loop did not preview frame {frame_index}")

    def _labels(self, view):
        return [item.shape().label for item in view.label_list]

    def _open_drawing(self):
        shape = Shape(shape_type="polygon", flags={})
        shape.points = [
            QtCore.QPointF(4, 4),
            QtCore.QPointF(18, 10),
        ]
        return shape

    def _mark_checked(self, view, indexes):
        for index in indexes:
            item = view.file_list_widget.item(index)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, True)

    @contextmanager
    def _answer_modal(self, button, delay_ms=100):
        """Click a real QMessageBox after ``delay_ms`` inside its event loop."""

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

    def _assert_stopped_on(self, view, frames, index):
        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)
        self.assertFalse(view.playback_control.loop_button.isChecked())
        self.assertEqual(Path(view.filename), frames[index])
        self.assertEqual(Path(view.image_path), frames[index])
        self.assertEqual(view.image.width(), 40 + index)
        self.assertEqual(view.canvas.pixmap.width(), 40 + index)
        self.assertEqual(view.playback_control.current_frame, index)
        self.assertTrue(view.canvas.isEnabled())

    def test_next_and_previous_follow_the_preview_frame(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        self.assertEqual(view._playback_anchor_index, 10)
        self.assertEqual(view.image.width(), 50)
        self.assertEqual(view.canvas.pixmap.width(), 55)
        self.assertEqual(self._labels(view), ["frame-10"])

        view.playback_control.next_button.click()
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 16)
        self.assertEqual(self._labels(view), ["frame-16"])
        self.assertEqual(self._snapshot(frames), before)

        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view.playback_control.prev_button.click()
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 14)
        self.assertEqual(self._labels(view), ["frame-14"])
        self.assertEqual(self._snapshot(frames), before)

    def test_steps_at_loop_window_and_sequence_ends(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 5)
        self.assertEqual(view._playback_anchor_index, 10)

        view.playback_control.prev_button.click()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 4)
        self.assertEqual(self._labels(view), ["frame-04"])
        self.assertEqual(self._snapshot(frames), before)

        self._show_frame(view, frames, 13)
        self._start_loop(view, radius=3)
        self._advance_loop_to(view, 16)
        self.assertFalse(view.playback_control.next_button.isEnabled())
        view._playback_step(1)
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 16)
        self.assertEqual(self._labels(view), ["frame-16"])

        view.playback_control.prev_button.click()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 15)
        self.assertEqual(self._labels(view), ["frame-15"])

        self._show_frame(view, frames, 3)
        self._start_loop(view, radius=3)
        self._advance_loop_to(view, 0)
        self.assertFalse(view.playback_control.prev_button.isEnabled())
        view._playback_step(-1)
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 0)
        self.assertEqual(self._labels(view), ["frame-00"])
        self.assertEqual(self._snapshot(frames), before)

    def test_anchor_edit_is_saved_before_step_loads_target(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view.canvas.shapes[0].label = "edited-anchor"
        view.set_dirty()
        self.assertTrue(view.dirty)
        self.assertEqual(self._snapshot(frames), before)

        save = QtWidgets.QMessageBox.StandardButton.Save
        with self._answer_modal(save, delay_ms=80) as state:
            view.playback_control.next_button.click()
        self.assertEqual(state["clicked"], 1)
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 16)
        self.assertFalse(view.dirty)
        self.assertEqual(self._labels(view), ["frame-16"])
        anchor = json.loads(
            frames[10].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(anchor["shapes"][0]["label"], "edited-anchor")
        self.assertEqual(anchor["imagePath"], frames[10].name)
        self.assertEqual(anchor["imageWidth"], 50)
        self.assertEqual(anchor["imageHeight"], 24)
        target = json.loads(
            frames[16].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(target["shapes"][0]["label"], "frame-16")
        for path in frames:
            if path == frames[10]:
                continue
            self.assertEqual(self._snapshot(frames)[path.name], before[path.name])

    def test_keep_prev_does_not_copy_anchor_shapes_onto_the_target(self):
        view, frames = self._open_sequence(
            auto_save=True, keep_prev=True, empty_indexes={16}
        )
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)

        view.playback_control.next_button.click()
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 16)
        self.assertEqual(self._labels(view), [])
        self.assertFalse(view.dirty)
        self.assertEqual(self._snapshot(frames), before)
        target = json.loads(
            frames[16].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(target["shapes"], [])
        self.assertEqual(target["imagePath"], frames[16].name)

    def test_idle_sequence_and_absolute_seek_keep_their_targets(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)

        self._show_frame(view, frames, 4)
        view.playback_control.next_button.click()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 5)
        self.assertEqual(self._labels(view), ["frame-05"])
        view.playback_control.prev_button.click()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 4)

        self._show_frame(view, frames, 0)
        self.assertFalse(view.playback_control.prev_button.isEnabled())
        view._playback_step(-1)
        self._assert_stopped_on(view, frames, 0)
        self._show_frame(view, frames, FRAME_COUNT - 1)
        self.assertFalse(view.playback_control.next_button.isEnabled())
        view._playback_step(1)
        self._assert_stopped_on(view, frames, FRAME_COUNT - 1)
        self.assertEqual(self._snapshot(frames), before)

        self._show_frame(view, frames, 0)
        view.toggle_sequence_playback(True)
        self.assertEqual(view._playback_mode, view.PLAYBACK_SEQUENCE)
        view._on_playback_tick()
        view._on_playback_tick()
        self.assertEqual(Path(view.filename), frames[2])
        view.playback_control.next_button.click()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 3)
        self.assertEqual(self._labels(view), ["frame-03"])
        view.playback_control.prev_button.click()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 2)
        self.assertEqual(self._snapshot(frames), before)

        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view._playback_seek(7)
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 7)
        self.assertEqual(self._labels(view), ["frame-07"])
        self.assertEqual(self._snapshot(frames), before)

        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view._playback_seek(15)
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 15)
        self.assertEqual(self._labels(view), ["frame-15"])

        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view._playback_seek(10)
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 10)
        self.assertEqual(self._labels(view), ["frame-10"])
        self.assertEqual(self._snapshot(frames), before)

    def test_keyboard_shortcuts_follow_the_preview_frame(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)

        view.actions.open_next_image.trigger()
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 16)
        self.assertEqual(self._labels(view), ["frame-16"])
        self.assertEqual(self._snapshot(frames), before)

        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view.actions.open_prev_image.trigger()
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 14)
        self.assertEqual(self._labels(view), ["frame-14"])
        self.assertEqual(self._snapshot(frames), before)

    def test_keyboard_stays_on_the_preview_at_sequence_ends(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 13)
        self._start_loop(view, radius=3)
        self._advance_loop_to(view, 16)

        view.actions.open_next_image.trigger()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 16)
        self.assertEqual(self._labels(view), ["frame-16"])

        self._show_frame(view, frames, 3)
        self._start_loop(view, radius=3)
        self._advance_loop_to(view, 0)
        view.actions.open_prev_image.trigger()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 0)
        self.assertEqual(self._labels(view), ["frame-00"])
        self.assertEqual(self._snapshot(frames), before)

    def test_keyboard_saves_anchor_edit_on_the_preview_neighbor(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view.canvas.shapes[0].label = "edited-anchor"
        view.set_dirty()

        save = QtWidgets.QMessageBox.StandardButton.Save
        with self._answer_modal(save, delay_ms=80) as state:
            view.actions.open_next_image.trigger()
        self.assertEqual(state["clicked"], 1)
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 16)
        self.assertFalse(view.dirty)
        self.assertEqual(self._labels(view), ["frame-16"])
        anchor = json.loads(
            frames[10].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(anchor["shapes"][0]["label"], "edited-anchor")
        self.assertEqual(anchor["imagePath"], frames[10].name)
        self.assertEqual(anchor["imageWidth"], 50)
        target = json.loads(
            frames[16].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(target["shapes"][0]["label"], "frame-16")
        self.assertEqual(target["imagePath"], frames[16].name)
        for path in frames:
            if path == frames[10]:
                continue
            self.assertEqual(
                self._snapshot(frames)[path.name], before[path.name]
            )

    def test_loop_seek_does_not_copy_anchor_shapes_onto_an_empty_frame(self):
        view, frames = self._open_sequence(
            auto_save=True, keep_prev=True, empty_indexes={7}
        )
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        self.assertEqual(self._labels(view), ["frame-10"])

        view._playback_seek(7)
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 7)
        self.assertEqual(self._labels(view), [])
        self.assertFalse(view.dirty)
        target = json.loads(
            frames[7].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(target["shapes"], [])
        self.assertEqual(target["imagePath"], frames[7].name)
        self.assertEqual(self._snapshot(frames), before)

    def test_idle_keep_prev_still_copies_onto_an_empty_frame(self):
        view, frames = self._open_sequence(
            auto_save=True, keep_prev=True, empty_indexes={5, 6}
        )
        before = self._snapshot(frames)
        self._show_frame(view, frames, 4)

        view.actions.open_next_image.trigger()
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 5)
        self.assertEqual(self._labels(view), ["frame-04"])
        copied = json.loads(
            frames[5].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(copied["shapes"][0]["label"], "frame-04")
        self.assertEqual(copied["imagePath"], frames[5].name)
        for path in frames:
            if path == frames[5]:
                continue
            self.assertEqual(
                self._snapshot(frames)[path.name], before[path.name]
            )

        self._show_frame(view, frames, 4)
        view.playback_control.next_button.click()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 5)
        self.assertEqual(self._labels(view), ["frame-04"])

        self._show_frame(view, frames, 4)
        view._playback_seek(6)
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 6)
        self.assertEqual(self._labels(view), ["frame-04"])
        sought = json.loads(
            frames[6].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(sought["shapes"][0]["label"], "frame-04")
        self.assertEqual(sought["imagePath"], frames[6].name)

    def test_cancelled_navigation_keeps_the_shape_being_drawn(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 4)
        drawing = self._open_drawing()
        view.canvas.current = drawing
        view.set_dirty()
        self.assertTrue(view.dirty)

        with mock.patch.object(
            QtWidgets.QMessageBox,
            "question",
            return_value=QtWidgets.QMessageBox.StandardButton.Cancel,
        ) as question:
            view.actions.open_next_image.trigger()
            view.actions.open_prev_image.trigger()
            view._playback_seek(9)
            self.app.processEvents()

        self.assertGreaterEqual(question.call_count, 3)
        self.assertIs(view.canvas.current, drawing)
        self.assertTrue(view.dirty)
        self.assertEqual(Path(view.filename), frames[4])
        self.assertEqual(self._labels(view), ["frame-04"])
        self.assertEqual(self._snapshot(frames), before)

    def test_cancelled_loop_navigation_keeps_the_drawing(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        drawing = self._open_drawing()
        view.canvas.current = drawing
        view.dirty = True

        with mock.patch.object(
            QtWidgets.QMessageBox,
            "question",
            return_value=QtWidgets.QMessageBox.StandardButton.Cancel,
        ) as question:
            view.actions.open_next_image.trigger()
            view._playback_seek(7)

        self.assertEqual(question.call_count, 0)
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertTrue(view.playback_timer.isActive())
        self.assertEqual(view._playback_display_index, 15)
        self.assertEqual(view._playback_anchor_index, 10)
        self.assertEqual(Path(view.filename), frames[10])
        self.assertIs(view.canvas.current, drawing)
        self.assertEqual(self._labels(view), ["frame-10"])
        self.assertTrue(view.dirty)
        self.assertEqual(self._snapshot(frames), before)

    def test_open_loop_drawing_is_not_dropped_by_navigation(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        drawing = self._open_drawing()
        view.canvas.current = drawing
        self.assertFalse(view.dirty)

        view.actions.open_next_image.trigger()
        view.playback_control.next_button.click()
        view._playback_seek(7)

        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertTrue(view.playback_timer.isActive())
        self.assertEqual(view._playback_display_index, 15)
        self.assertEqual(view._playback_anchor_index, 10)
        self.assertEqual(Path(view.filename), frames[10])
        self.assertIs(view.canvas.current, drawing)
        self.assertEqual(self._labels(view), ["frame-10"])
        self.assertEqual(self._snapshot(frames), before)

    def test_sequence_keyboard_steps_from_the_open_frame(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 0)
        view.toggle_sequence_playback(True)
        view._on_playback_tick()
        view._on_playback_tick()
        self.assertEqual(Path(view.filename), frames[2])

        view.actions.open_next_image.trigger()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 3)
        self.assertEqual(self._labels(view), ["frame-03"])

        view.actions.open_prev_image.trigger()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 2)
        self.assertEqual(self._labels(view), ["frame-02"])
        self.assertEqual(self._snapshot(frames), before)

    def test_real_timer_keeps_a_dirty_drawing_without_a_dialog(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        drawing = self._open_drawing()
        view.canvas.current = drawing
        view.dirty = True
        cancel = QtWidgets.QMessageBox.StandardButton.Cancel

        with self._answer_modal(cancel, delay_ms=100) as state:
            view.actions.open_next_image.trigger()
            view.actions.open_prev_image.trigger()
            view.playback_control.next_button.click()
            view.playback_control.prev_button.click()
            view.playback_control.seek_requested.emit(7)

        self.assertEqual(state["seen"], 0)
        self.assertEqual(state["clicked"], 0)
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertTrue(view.playback_timer.isActive())
        self.assertEqual(view._playback_anchor_index, 10)
        self.assertEqual(Path(view.filename), frames[10])
        self.assertIs(view.canvas.current, drawing)
        self.assertEqual(self._labels(view), ["frame-10"])
        self.assertTrue(view.dirty)
        self.assertEqual(self._snapshot(frames), before)

    def test_real_timer_keeps_a_dirty_brush_without_a_dialog(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view.canvas.is_brush_mode = True
        view.dirty = True
        cancel = QtWidgets.QMessageBox.StandardButton.Cancel

        with self._answer_modal(cancel, delay_ms=100) as state:
            view.actions.open_next_image.trigger()
            view.playback_control.next_button.click()
            view.playback_control.seek_requested.emit(7)

        self.assertEqual(state["seen"], 0)
        self.assertTrue(view.canvas.is_brush_mode)
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertTrue(view.playback_timer.isActive())
        self.assertIn(view._playback_display_index, range(5, 16))
        self.assertEqual(Path(view.filename), frames[10])
        self.assertTrue(view.dirty)
        self.assertEqual(self._snapshot(frames), before)

    def test_real_save_dialog_cancel_keeps_dirty_loop_edits(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view.canvas.shapes[0].label = "edited-anchor"
        view.set_dirty()
        ticks = {"count": 0}
        view.playback_timer.timeout.connect(lambda: ticks.__setitem__(
            "count", ticks["count"] + 1
        ))
        cancel = QtWidgets.QMessageBox.StandardButton.Cancel
        actions = (
            lambda: view.actions.open_next_image.trigger(),
            lambda: view.actions.open_prev_image.trigger(),
            lambda: view.playback_control.next_button.click(),
            lambda: view.playback_control.prev_button.click(),
            lambda: view.playback_control.seek_requested.emit(7),
        )

        for action in actions:
            ticks["count"] = 0
            with self._answer_modal(cancel, delay_ms=100) as state:
                action()
            self.assertEqual(state["seen"], 1)
            self.assertEqual(state["clicked"], 1)
            self.assertGreater(ticks["count"], 0)
            self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
            self.assertTrue(view.playback_timer.isActive())
            self.assertEqual(view._playback_display_index, 15)
            self.assertEqual(view._playback_anchor_index, 10)
            self.assertEqual(Path(view.filename), frames[10])
            self.assertEqual(self._labels(view), ["edited-anchor"])
            self.assertTrue(view.dirty)
            self.assertEqual(self._snapshot(frames), before)

    def test_real_save_dialog_discard_steps_from_the_preview(self):
        view, frames = self._open_sequence(auto_save=False, keep_prev=True)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view.canvas.shapes[0].label = "edited-anchor"
        view.set_dirty()
        discard = QtWidgets.QMessageBox.StandardButton.Discard

        with self._answer_modal(discard, delay_ms=80) as state:
            view.playback_control.next_button.click()

        self.assertEqual(state["clicked"], 1)
        self._assert_stopped_on(view, frames, 16)
        self.assertEqual(self._labels(view), ["frame-16"])
        anchor = json.loads(
            frames[10].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(anchor["shapes"][0]["label"], "frame-10")
        self.assertEqual(anchor["imagePath"], frames[10].name)
        self.assertEqual(self._snapshot(frames), before)

    def test_unchecked_navigation_uses_the_loop_preview(self):
        view, frames = self._open_sequence(keep_prev=True, empty_indexes={16})
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 14)
        self._mark_checked(view, [15])

        view.actions.open_next_unchecked_image.trigger()
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 16)
        self.assertEqual(self._labels(view), [])
        target = json.loads(
            frames[16].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(target["shapes"], [])
        self.assertEqual(target["imagePath"], frames[16].name)
        self.assertEqual(self._snapshot(frames), before)

        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        self._mark_checked(view, [14])
        view.actions.open_prev_unchecked_image.trigger()
        self.app.processEvents()

        self._assert_stopped_on(view, frames, 13)
        self.assertEqual(self._labels(view), ["frame-13"])
        self.assertEqual(self._snapshot(frames), before)

    def test_unchecked_navigation_cancel_and_drawing_stay_on_the_preview(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        self._show_frame(view, frames, 10)
        self._start_loop(view, radius=5)
        self._advance_loop_to(view, 15)
        view.canvas.shapes[0].label = "edited-anchor"
        view.set_dirty()
        cancel = QtWidgets.QMessageBox.StandardButton.Cancel

        with self._answer_modal(cancel, delay_ms=100) as state:
            view.actions.open_next_unchecked_image.trigger()
            view.actions.open_prev_unchecked_image.trigger()

        self.assertEqual(state["clicked"], 2)
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertEqual(view._playback_display_index, 15)
        self.assertEqual(self._labels(view), ["edited-anchor"])
        self.assertTrue(view.dirty)
        self.assertEqual(self._snapshot(frames), before)

        drawing = self._open_drawing()
        view.canvas.current = drawing
        with self._answer_modal(cancel, delay_ms=100) as state:
            view.actions.open_next_unchecked_image.trigger()
            view.actions.open_prev_unchecked_image.trigger()
        self.assertEqual(state["seen"], 0)
        self.assertIs(view.canvas.current, drawing)
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertEqual(Path(view.filename), frames[10])
        self.assertEqual(self._snapshot(frames), before)

    def test_unchecked_navigation_outside_a_loop_uses_the_open_frame(self):
        view, frames = self._open_sequence()
        before = self._snapshot(frames)
        self._show_frame(view, frames, 0)
        self._mark_checked(view, [1])

        view.actions.open_next_unchecked_image.trigger()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 2)
        self.assertEqual(self._labels(view), ["frame-02"])

        self._mark_checked(view, [1])
        view.actions.open_prev_unchecked_image.trigger()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 0)
        self.assertEqual(self._snapshot(frames), before)

        self._show_frame(view, frames, 0)
        view.toggle_sequence_playback(True)
        view._on_playback_tick()
        view._on_playback_tick()
        self.assertEqual(Path(view.filename), frames[2])
        self._mark_checked(view, [3])
        view.actions.open_next_unchecked_image.trigger()
        self.app.processEvents()
        self._assert_stopped_on(view, frames, 4)
        self.assertEqual(self._labels(view), ["frame-04"])
        self.assertEqual(self._snapshot(frames), before)
