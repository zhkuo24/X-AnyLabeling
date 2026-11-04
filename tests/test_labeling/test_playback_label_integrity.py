import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SETTINGS_ROOT = tempfile.mkdtemp(prefix="xany-playback-label-settings-")
os.environ["XDG_CONFIG_HOME"] = _SETTINGS_ROOT

try:
    from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

    from anylabeling import config as anylabeling_config
    from anylabeling.config import get_config, set_work_directory
    from anylabeling.resources import resources  # noqa: F401
    from anylabeling.views.mainwindow import MainWindow

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False


def _sha256(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_frame(path, width, height, shapes=None):
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(width % 255, height % 255, 90))
    if not image.save(str(path)):
        raise AssertionError(f"failed to write {path}")
    payload = {
        "version": "5.0.0",
        "flags": {"reviewed": True},
        "checked": False,
        "shapes": (
            shapes
            if shapes is not None
            else [
                {
                    "label": "lesion",
                    "points": [[30, 20], [10, 20], [10, 5], [30, 5]],
                    "group_id": None,
                    "description": "",
                    "shape_type": "rectangle",
                    "flags": {},
                    "mask": None,
                }
            ]
        ),
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


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for playback label integrity tests"
)
class TestPlaybackLabelIntegrity(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="xany-playback-labels-"
        )
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
        from anylabeling.label_crypto import clear_key_cache

        clear_key_cache()

    def _open_sequence(self, sizes, auto_save=True, keep_prev=False):
        root = Path(self.temp_dir.name) / f"case-{len(self.windows)}"
        frame_dir = root / "frames"
        frame_dir.mkdir(parents=True)
        frames = []
        for index, (width, height) in enumerate(sizes):
            path = frame_dir / f"{index:03d}.png"
            _write_frame(path, width, height)
            frames.append(path)
        set_work_directory(str(root / "settings"))
        anylabeling_config.current_config_file = "{}"
        config = get_config("{}")
        config["auto_save"] = auto_save
        config["keep_prev"] = keep_prev
        config["exif_scan_enabled"] = False
        from anylabeling.label_crypto import generate_key_file

        key_path = root / "project.key"
        generate_key_file(str(key_path))
        config["label_key_file"] = str(key_path)
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
        snap = {}
        for path in frames:
            for suffix in (".json", ".xalenc"):
                label = path.with_suffix(suffix)
                key = f"{path.name}{suffix}"
                snap[key] = _sha256(label) if label.is_file() else None
        return snap

    def _label(self, frame):
        from anylabeling.views.labeling.label_file import LabelFile

        encrypted = frame.with_suffix(".xalenc")
        label = (
            encrypted if encrypted.is_file() else frame.with_suffix(".json")
        )
        return LabelFile.read_document(str(label))

    def test_loop_and_sequence_advance_do_not_rewrite_label_files(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50), (48, 32)]
        )
        original = self._snapshot(frames)
        anchor = Path(view.filename)

        view.toggle_loop_playback(True)
        self.assertTrue(view.playback_timer.isActive())
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        # Starting a loop reloads the anchor from disk, so shape identity
        # changes once. Preview ticks must keep that same object: replacing
        # it would drop an in-progress edit.
        anchor_shape = view.canvas.shapes[0]
        anchor_points = [
            (point.x(), point.y()) for point in anchor_shape.points
        ]
        for _ in range(5):
            view._on_playback_tick()
            self.assertEqual(Path(view.filename), anchor)
            self.assertEqual(Path(view.image_path), anchor)
            self.assertEqual(view.image.width(), 48)
            self.assertEqual(view.image.height(), 32)
            self.assertEqual(len(view.canvas.shapes), 1)
            self.assertIs(view.canvas.shapes[0], anchor_shape)
            self.assertEqual(anchor_shape.label, "lesion")
            self.assertEqual(
                [(point.x(), point.y()) for point in anchor_shape.points],
                anchor_points,
            )
            self.assertFalse(view.dirty)
        self.assertNotEqual(
            view._playback_display_index,
            view.fn_to_index[str(anchor)],
        )

        view.playback_control.loop_button.click()
        self.app.processEvents()
        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)
        self.assertEqual(self._snapshot(frames), original)

        view.toggle_sequence_playback(True)
        self.assertTrue(view.playback_timer.isActive())
        for _ in range(6):
            if not view.playback_timer.isActive():
                break
            view._on_playback_tick()
        view._stop_playback()
        self.assertEqual(self._snapshot(frames), original)

    def test_edit_during_loop_updates_only_the_anchor_document(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)]
        )
        neighbors = {
            path.name: path.with_suffix(".json").read_bytes()
            for path in frames[1:]
        }
        view.toggle_loop_playback(True)
        for _ in range(5):
            view._on_playback_tick()
        self.assertEqual(Path(view.filename).name, frames[0].name)
        self.assertNotEqual(
            view._playback_display_index,
            view.fn_to_index[str(frames[0])],
        )

        view.canvas.shapes[0].label = "edited-during-loop"
        view.set_dirty()
        self.assertTrue(frames[0].with_suffix(".xalenc").is_file())
        self.assertFalse(frames[0].with_suffix(".json").is_file())
        for path in frames[1:]:
            self.assertFalse(path.with_suffix(".xalenc").is_file())
        anchor_label = self._label(frames[0])
        self.assertEqual(anchor_label["imagePath"], frames[0].name)
        self.assertEqual(anchor_label["imageWidth"], 48)
        self.assertEqual(anchor_label["imageHeight"], 32)
        self.assertEqual(
            anchor_label["shapes"][0]["label"], "edited-during-loop"
        )
        for path in frames[1:]:
            self.assertEqual(
                path.with_suffix(".json").read_bytes(),
                neighbors[path.name],
            )

        for _ in range(3):
            view._on_playback_tick()
        view.toggle_loop_playback(False)
        anchor_label = self._label(frames[0])
        self.assertEqual(anchor_label["imagePath"], frames[0].name)
        self.assertEqual(anchor_label["imageWidth"], 48)
        self.assertEqual(anchor_label["imageHeight"], 32)
        for path in frames[1:]:
            self.assertEqual(
                path.with_suffix(".json").read_bytes(),
                neighbors[path.name],
            )

    def test_stopping_loop_does_not_rewrite_a_file_saved_at_start(self):
        _window, view, frames = self._open_sequence(
            [(48, 32), (64, 40), (80, 50)],
            auto_save=False,
        )
        before = self._snapshot(frames)
        view.set_dirty()
        self.assertTrue(view.dirty)
        self.assertEqual(self._snapshot(frames), before)

        view.toggle_loop_playback(True)
        self.assertFalse(view.dirty)
        saved_at_start = self._snapshot(frames)
        anchor_json = f"{frames[0].name}.json"
        anchor_enc = f"{frames[0].name}.xalenc"
        self.assertIsNotNone(before[anchor_json])
        self.assertIsNone(before[anchor_enc])
        self.assertIsNone(saved_at_start[anchor_json])
        self.assertIsNotNone(saved_at_start[anchor_enc])
        for path in frames[1:]:
            self.assertEqual(
                saved_at_start[f"{path.name}.json"],
                before[f"{path.name}.json"],
            )
            self.assertIsNone(saved_at_start[f"{path.name}.xalenc"])
        self.assertEqual(self._label(frames[0])["imagePath"], frames[0].name)
        self.assertEqual(self._label(frames[0])["imageWidth"], 48)
        self.assertEqual(self._label(frames[0])["imageHeight"], 32)

        for _ in range(6):
            view._on_playback_tick()
        view.toggle_loop_playback(False)
        self.assertEqual(self._snapshot(frames), saved_at_start)

    def test_one_frame_loop_click_explains_why_playback_does_not_start(self):
        window, view, _frames = self._open_sequence([(32, 32)])
        self.assertTrue(view.playback_control.loop_button.isEnabled())
        self.assertTrue(view.playback_control.play_button.isEnabled())

        view.playback_control.loop_button.click()
        self.app.processEvents()

        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)
        self.assertFalse(view.playback_control.loop_button.isChecked())
        self.assertIn("two frames", window.statusBar().currentMessage())

    def test_loop_release_outside_the_button_still_stops_playback(self):
        _window, view, _frames = self._open_sequence(
            [(32, 32), (32, 32), (32, 32)]
        )
        button = view.playback_control.loop_button
        button.click()
        self.app.processEvents()
        self.assertTrue(view.playback_timer.isActive())

        center = button.rect().center()
        QtTest.QTest.mousePress(
            button,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            center,
        )
        self.app.processEvents()
        self.assertFalse(view.playback_timer.isActive())

        QtTest.QTest.mouseRelease(
            button,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            QtCore.QPoint(-40, -40),
        )
        self.app.processEvents()
        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)


def tearDownModule():
    shutil.rmtree(_SETTINGS_ROOT, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
