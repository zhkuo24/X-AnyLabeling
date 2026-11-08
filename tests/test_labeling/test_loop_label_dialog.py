import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SETTINGS_ROOT = tempfile.mkdtemp(prefix="xany-loop-dialog-settings-")
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


def _sha256(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rectangle(label="lesion"):
    return {
        "label": label,
        "points": [[30, 20], [10, 20], [10, 5], [30, 5]],
        "group_id": None,
        "description": "",
        "shape_type": "rectangle",
        "flags": {},
        "mask": None,
    }


def _write_frame(path, width, height, shapes=None):
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(width % 255, height % 255, 90))
    if not image.save(str(path)):
        raise AssertionError(f"failed to write {path}")
    if shapes is None:
        shapes = [_rectangle()]
    payload = {
        "version": "5.0.0",
        "flags": {"reviewed": True},
        "checked": False,
        "shapes": shapes,
        "imagePath": Path(path).name,
        "imageData": None,
        "imageHeight": height,
        "imageWidth": width,
        "description": f"note-{Path(path).stem}",
    }
    Path(path).with_suffix(".json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def _point_tuples(shape):
    return [(point.x(), point.y()) for point in shape.points]


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for loop label-dialog tests"
)
class TestLoopLabelDialog(unittest.TestCase):
    """Loop playback must not drop or retarget a shape while its label
    dialog is inside QDialog.exec, and must not discard an in-progress
    shape when loop playback is started."""

    NEW_POINTS = [(10.0, 12.0), (40.0, 12.0), (40.0, 28.0), (10.0, 28.0)]

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="xany-loop-dialog-"
        )
        self.windows = []
        self.app = QtWidgets.QApplication.instance()
        if self.app is None:
            self.app = QtWidgets.QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self._screen_at = QtWidgets.QApplication.screenAt
        QtWidgets.QApplication.screenAt = staticmethod(self._screen_or_primary)

    def tearDown(self):
        QtWidgets.QApplication.screenAt = self._screen_at
        for window in self.windows:
            view = window.labeling_widget.view
            if view.playback_timer.isActive() or view._playback_mode:
                view._stop_playback()
            window.hide()
            window.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    @staticmethod
    def _screen_or_primary(pos):
        screen = QtGui.QGuiApplication.screenAt(pos)
        if screen is None:
            screen = QtGui.QGuiApplication.primaryScreen()
        return screen

    def _open_sequence(
        self, sizes, auto_save=True, anchor_shapes=None
    ):
        root = Path(self.temp_dir.name) / f"case-{len(self.windows)}"
        frame_dir = root / "frames"
        frame_dir.mkdir(parents=True)
        frames = []
        for index, (width, height) in enumerate(sizes):
            path = frame_dir / f"{index:03d}.png"
            shapes = anchor_shapes if index == 0 else None
            _write_frame(path, width, height, shapes=shapes)
            frames.append(path)
        set_work_directory(str(root / "settings"))
        anylabeling_config.current_config_file = "{}"
        config = get_config("{}")
        config["auto_save"] = auto_save
        config["keep_prev"] = False
        config["exif_scan_enabled"] = False
        config["display_label_popup"] = True
        config["auto_use_last_label"] = False
        config["validate_label"] = None
        window = MainWindow(self.app, config=config, filename=str(frame_dir))
        window.resize(1000, 720)
        window.show()
        self.app.processEvents()
        self.windows.append(window)
        view = window.labeling_widget.view
        self.assertEqual(len(view.image_list), len(frames))
        self.assertIsNotNone(view.filename)
        return window, view, frames

    def _snapshot(self, frames):
        return {
            path.name: _sha256(path.with_suffix(".json")) for path in frames
        }

    def _label(self, frame):
        return json.loads(
            frame.with_suffix(".json").read_text(encoding="utf-8")
        )

    def _labels(self, view):
        return [item.shape().label for item in view.label_list]

    def _start_loop_on_neighbor(self, view):
        anchor = Path(view.filename)
        anchor_index = view.fn_to_index[str(anchor)]
        view.toggle_loop_playback(True)
        self.assertTrue(view.playback_timer.isActive())
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        view._on_playback_tick()
        self.assertNotEqual(view._playback_display_index, anchor_index)
        self.assertTrue(view.playback_timer.isActive())
        return anchor

    def _start_open_polygon(self, view, points):
        """Leave a polygon in progress the way a real press-and-move does.

        ``canvas.line`` must contain the segment from the last vertex to the
        cursor. Painting an open polygon without that segment indexes
        ``line[1]`` and aborts inside Qt's paint event.
        """

        view.canvas.create_mode = "polygon"
        current = Shape(label="", shape_type="polygon")
        for x_pos, y_pos in points:
            current.add_point(QtCore.QPointF(x_pos, y_pos))
        view.canvas.current = current
        last = current.points[-1]
        cursor = QtCore.QPointF(last.x() + 4, last.y() + 4)
        view.canvas.line.points = [last, cursor]
        return current

    def _append_finished_shape(self, view):
        shape = Shape(label="", shape_type="rectangle")
        for x_pos, y_pos in self.NEW_POINTS:
            shape.add_point(QtCore.QPointF(x_pos, y_pos))
        shape.close()
        view.canvas.shapes.append(shape)
        view.canvas.store_shapes()
        view.canvas.current = None
        return shape

    def _finish_label_dialog(self, view, accept_label):
        """Run ``new_shape`` while the real playback timer is active.

        The closer waits until that timer has been delivered inside the
        dialog's nested event loop, then accepts or rejects the dialog.
        """

        state = {"ticks": 0, "timeout": False}

        def count_tick():
            state["ticks"] += 1

        view.playback_timer.timeout.connect(count_tick)

        def close_when_timer_fired(remaining=200):
            dialog = view.label_dialog
            if remaining <= 0:
                state["timeout"] = True
                if dialog.isVisible():
                    dialog.reject()
                return
            if dialog.isVisible() and state["ticks"] >= 1:
                if accept_label:
                    dialog.edit.setText(accept_label)
                    dialog.accept()
                else:
                    dialog.reject()
                return
            QtCore.QTimer.singleShot(
                15, lambda: close_when_timer_fired(remaining - 1)
            )

        QtCore.QTimer.singleShot(15, close_when_timer_fired)
        view.new_shape()
        self.app.processEvents()
        self.assertFalse(
            state["timeout"],
            "playback timer was not delivered during the label dialog",
        )
        self.assertGreaterEqual(state["ticks"], 1)
        return state["ticks"]

    def _run_blocking_widget(self, view, widget_factory):
        state = {"ticks": 0, "timeout": False}

        def count_tick():
            state["ticks"] += 1

        view.playback_timer.timeout.connect(count_tick)
        widget = widget_factory()

        def close_when_timer_fired(remaining=200):
            if remaining <= 0:
                state["timeout"] = True
                widget.close()
                return
            if state["ticks"] >= 1:
                widget.close()
                return
            QtCore.QTimer.singleShot(
                15, lambda: close_when_timer_fired(remaining - 1)
            )

        QtCore.QTimer.singleShot(15, close_when_timer_fired)
        if isinstance(widget, QtWidgets.QMenu):
            screen = QtGui.QGuiApplication.primaryScreen()
            widget.exec(screen.geometry().center())
        else:
            widget.exec()
        self.app.processEvents()
        self.assertFalse(state["timeout"])
        self.assertGreaterEqual(state["ticks"], 1)

    def _assert_anchor_document(self, view, anchor, frames, before, labels):
        self.assertEqual(Path(view.filename), anchor)
        self.assertEqual(Path(view.image_path), anchor)
        self.assertIsNone(view._playback_mode)
        self.assertFalse(view.playback_timer.isActive())
        data = self._label(anchor)
        self.assertEqual(data["imagePath"], anchor.name)
        self.assertEqual(data["imageWidth"], 48)
        self.assertEqual(data["imageHeight"], 32)
        self.assertEqual([shape["label"] for shape in data["shapes"]], labels)
        for frame in frames[1:]:
            self.assertEqual(
                self._snapshot([frame])[frame.name], before[frame.name]
            )

    def test_confirm_keeps_new_shape_and_existing_anchor_shape(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50), (48, 32)],
            auto_save=True,
        )
        anchor = self._start_loop_on_neighbor(view)
        existing = view.canvas.shapes[0]
        existing_points = _point_tuples(existing)
        before = self._snapshot(frames)
        new_shape = self._append_finished_shape(view)

        self._finish_label_dialog(view, "freshmark")

        self.assertIs(existing, view.canvas.shapes[0])
        self.assertEqual(existing.label, "lesion")
        self.assertEqual(_point_tuples(existing), existing_points)
        self.assertIs(new_shape, view.canvas.shapes[1])
        self.assertEqual(new_shape.label, "freshmark")
        self.assertEqual(_point_tuples(new_shape), self.NEW_POINTS)
        self.assertEqual(self._labels(view), ["lesion", "freshmark"])
        self.assertFalse(view.dirty)
        self.assertEqual(view.canvas.pixmap.width(), 48)
        self.assertEqual(view.canvas.pixmap.height(), 32)
        fresh = self._label(anchor)["shapes"][1]
        self.assertEqual(fresh["label"], "freshmark")
        self.assertEqual(
            sorted({point[0] for point in fresh["points"]}), [10.0, 40.0]
        )
        self.assertEqual(
            sorted({point[1] for point in fresh["points"]}), [12.0, 28.0]
        )
        self._assert_anchor_document(
            view, anchor, frames, before, ["lesion", "freshmark"]
        )

    def test_confirm_on_empty_anchor_creates_only_the_new_shape(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)],
            auto_save=True,
            anchor_shapes=[],
        )
        anchor = self._start_loop_on_neighbor(view)
        self.assertEqual(view.canvas.shapes, [])
        before = self._snapshot(frames)
        new_shape = self._append_finished_shape(view)

        self._finish_label_dialog(view, "freshmark")

        self.assertEqual(view.canvas.shapes, [new_shape])
        self.assertEqual(new_shape.label, "freshmark")
        self.assertEqual(_point_tuples(new_shape), self.NEW_POINTS)
        self.assertEqual(self._labels(view), ["freshmark"])
        self._assert_anchor_document(
            view, anchor, frames, before, ["freshmark"]
        )

    def test_cancel_restores_drawing_without_touching_saved_shapes(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)],
            auto_save=True,
        )
        anchor = self._start_loop_on_neighbor(view)
        existing = view.canvas.shapes[0]
        existing_points = _point_tuples(existing)
        before = self._snapshot(frames)
        new_shape = self._append_finished_shape(view)

        self._finish_label_dialog(view, None)

        self.assertEqual(view.canvas.shapes, [existing])
        self.assertEqual(existing.label, "lesion")
        self.assertEqual(_point_tuples(existing), existing_points)
        self.assertIs(view.canvas.current, new_shape)
        self.assertEqual(_point_tuples(new_shape), self.NEW_POINTS)
        self.assertEqual(self._labels(view), ["lesion"])
        self.assertEqual(self._snapshot(frames), before)
        self.assertFalse(view.dirty)

    def test_cancel_on_empty_anchor_keeps_geometry_as_current_shape(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)],
            auto_save=False,
            anchor_shapes=[],
        )
        self._start_loop_on_neighbor(view)
        before = self._snapshot(frames)
        new_shape = self._append_finished_shape(view)

        self._finish_label_dialog(view, None)

        self.assertEqual(view.canvas.shapes, [])
        self.assertIs(view.canvas.current, new_shape)
        self.assertEqual(_point_tuples(new_shape), self.NEW_POINTS)
        self.assertEqual(self._labels(view), [])
        self.assertEqual(self._snapshot(frames), before)

    def test_confirm_without_autosave_dirties_anchor_and_skips_neighbors(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)],
            auto_save=False,
        )
        anchor = self._start_loop_on_neighbor(view)
        before = self._snapshot(frames)
        new_shape = self._append_finished_shape(view)

        self._finish_label_dialog(view, "freshmark")

        self.assertIs(new_shape, view.canvas.shapes[1])
        self.assertEqual(self._labels(view), ["lesion", "freshmark"])
        self.assertTrue(view.dirty)
        self.assertEqual(self._snapshot(frames), before)
        view.save_file()
        self.assertFalse(view.dirty)
        self._assert_anchor_document(
            view, anchor, frames, before, ["lesion", "freshmark"]
        )

    def test_modal_dialog_while_drawing_keeps_current_shape(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)]
        )
        anchor = self._start_loop_on_neighbor(view)
        existing = view.canvas.shapes[0]
        before = self._snapshot(frames)
        current = self._start_open_polygon(view, [(2, 3), (8, 3)])

        def make_dialog():
            dialog = QtWidgets.QDialog(view)
            dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
            dialog.setWindowTitle("blocking")
            return dialog

        self._run_blocking_widget(view, make_dialog)

        self.assertIs(view.canvas.current, current)
        self.assertEqual(_point_tuples(current), [(2.0, 3.0), (8.0, 3.0)])
        self.assertIs(view.canvas.shapes[0], existing)
        self.assertEqual(existing.label, "lesion")
        self.assertEqual(self._snapshot(frames), before)
        self.assertEqual(Path(view.filename), anchor)
        self.assertIsNone(view._playback_mode)
        self.assertEqual(view.canvas.pixmap.width(), 48)

    def test_popup_menu_while_drawing_keeps_current_shape(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)]
        )
        self._start_loop_on_neighbor(view)
        existing = view.canvas.shapes[0]
        before = self._snapshot(frames)
        current = self._start_open_polygon(view, [(4, 5)])

        def make_menu():
            menu = QtWidgets.QMenu(view)
            menu.addAction("noop")
            return menu

        self._run_blocking_widget(view, make_menu)

        self.assertIs(view.canvas.current, current)
        self.assertIs(view.canvas.shapes[0], existing)
        self.assertEqual(self._snapshot(frames), before)
        self.assertIsNone(view._playback_mode)

    def test_modal_without_uncommitted_geometry_returns_to_anchor(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)]
        )
        anchor = self._start_loop_on_neighbor(view)
        existing_points = _point_tuples(view.canvas.shapes[0])
        before = self._snapshot(frames)
        self.assertNotEqual(view.canvas.pixmap.width(), 48)

        def make_dialog():
            dialog = QtWidgets.QDialog(view)
            dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
            return dialog

        self._run_blocking_widget(view, make_dialog)

        # A clean stop still reloads the anchor, so the shape object is a
        # new instance loaded from disk. Its label, geometry, and file
        # bytes stay the saved anchor document.
        self.assertEqual(len(view.canvas.shapes), 1)
        self.assertEqual(view.canvas.shapes[0].label, "lesion")
        self.assertEqual(_point_tuples(view.canvas.shapes[0]), existing_points)
        self.assertEqual(self._labels(view), ["lesion"])
        self.assertEqual(view.canvas.pixmap.width(), 48)
        self.assertEqual(view.canvas.pixmap.height(), 32)
        self.assertEqual(self._snapshot(frames), before)
        self.assertEqual(Path(view.filename), anchor)
        self.assertEqual(Path(view.image_path), anchor)

    def test_starting_loop_while_drawing_keeps_the_open_shape(self):
        window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)]
        )
        existing = view.canvas.shapes[0]
        before = self._snapshot(frames)
        current = self._start_open_polygon(view, [(6, 7), (9, 7)])

        view.toggle_loop_playback(True)
        self.app.processEvents()

        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)
        self.assertIs(view.canvas.current, current)
        self.assertEqual(_point_tuples(current), [(6.0, 7.0), (9.0, 7.0)])
        self.assertIs(view.canvas.shapes[0], existing)
        self.assertEqual(self._snapshot(frames), before)
        self.assertIn(
            "current annotation",
            window.statusBar().currentMessage(),
        )

    def test_starting_loop_while_brush_editing_does_not_reload(self):
        window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)]
        )
        existing = view.canvas.shapes[0]
        before = self._snapshot(frames)
        view.canvas.is_brush_mode = True

        view.toggle_loop_playback(True)
        self.app.processEvents()

        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)
        self.assertTrue(view.canvas.is_brush_mode)
        self.assertIs(view.canvas.shapes[0], existing)
        self.assertEqual(self._snapshot(frames), before)
        self.assertIn(
            "current annotation",
            window.statusBar().currentMessage(),
        )


def tearDownModule():
    shutil.rmtree(_SETTINGS_ROOT, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
