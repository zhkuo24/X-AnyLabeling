import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SETTINGS_ROOT = tempfile.mkdtemp(prefix="xany-save-failure-settings-")
os.environ["XDG_CONFIG_HOME"] = _SETTINGS_ROOT

try:
    from PyQt6 import QtCore, QtGui, QtWidgets

    from anylabeling import config as anylabeling_config
    from anylabeling.config import get_config, set_work_directory
    from anylabeling.resources import resources  # noqa: F401
    from anylabeling.views.labeling.label_file import LabelFile
    from anylabeling.views.labeling.shape import Shape
    from anylabeling.views.mainwindow import MainWindow
    from tests.test_labeling.label_write_lock import (
        allow_label_writes,
        deny_label_writes,
    )

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False


def _sha256(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_frame(path, width, height):
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(width % 255, height % 255, 90))
    if not image.save(str(path)):
        raise AssertionError(f"failed to write {path}")
    payload = {
        "version": "5.0.0",
        "flags": {"reviewed": True},
        "checked": False,
        "shapes": [
            {
                "label": "lesion",
                "points": [[30, 20], [10, 20], [10, 5], [30, 5]],
                "group_id": None,
                "description": "",
                "shape_type": "rectangle",
                "flags": {},
                "mask": None,
            }
        ],
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


def _labels(view):
    return [item.shape().label for item in view.label_list]


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for save-failure dirty tests"
)
class TestSaveFailureDirty(unittest.TestCase):
    """A failed label write must stay dirty and must not be followed by a
    frame change or loop reload that drops the in-memory shapes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xany-save-failure-")
        self.windows = []
        self.app = QtWidgets.QApplication.instance()
        if self.app is None:
            self.app = QtWidgets.QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self._save_calls = 0
        self._original_save = LabelFile.save

        def counting_save(label_file, *args, **kwargs):
            self._save_calls += 1
            return self._original_save(label_file, *args, **kwargs)

        LabelFile.save = counting_save

    def tearDown(self):
        LabelFile.save = self._original_save
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

    def _open_sequence(self, auto_save):
        root = Path(self.temp_dir.name) / f"case-{len(self.windows)}"
        frame_dir = root / "frames"
        frame_dir.mkdir(parents=True)
        frames = []
        for index, (width, height) in enumerate(
            [(48, 32), (64, 40), (80, 50)]
        ):
            path = frame_dir / f"{index:03d}.png"
            _write_frame(path, width, height)
            frames.append(path)
        set_work_directory(str(root / "settings"))
        anylabeling_config.current_config_file = "{}"
        config = get_config("{}")
        config["auto_save"] = auto_save
        config["keep_prev"] = False
        config["exif_scan_enabled"] = False
        window = MainWindow(self.app, config=config, filename=str(frame_dir))
        window.resize(1000, 720)
        window.show()
        self.app.processEvents()
        self.windows.append(window)
        view = window.labeling_widget.view
        self.assertEqual(len(view.image_list), 3)
        self.assertEqual(Path(view.filename), frames[0])
        return view, frames

    def _snapshot(self, frames):
        return {
            path.name: _sha256(path.with_suffix(".json")) for path in frames
        }

    def _add_memory_shape(self, view, label="freshmark"):
        shape = Shape(label=label, shape_type="rectangle")
        for x_pos, y_pos in ((10, 12), (40, 12), (40, 28), (10, 28)):
            shape.add_point(QtCore.QPointF(x_pos, y_pos))
        shape.close()
        view.canvas.shapes.append(shape)
        view.add_label(shape)
        return shape

    def _lock(self, frames):
        deny_label_writes(frames[0].parent)

    def _unlock(self, frames):
        allow_label_writes(frames[0].parent)

    def _run_while_closing_save_errors(
        self, view, action, require_modal_tick=False
    ):
        """Run ``action`` and close the real save-error dialog.

        When ``require_modal_tick`` is set, the dialog stays up until the
        playback timer has been delivered inside that modal event loop.
        """

        state = {
            "modal_ticks": 0,
            "closed": 0,
            "timeout": False,
            "stop": False,
            "entered": False,
        }

        # The production slot is already connected. During a save-error
        # dialog the pre-fix tick re-enters ``_stop_playback`` and opens
        # another dialog. Allow that callback once so the loss is visible,
        # and refuse further re-entry so a regression fails instead of
        # hanging the suite.
        view.playback_timer.timeout.disconnect(view._on_playback_tick)

        def guarded_tick():
            modal = QtWidgets.QApplication.activeModalWidget() is not None
            if modal:
                state["modal_ticks"] += 1
                if state["entered"]:
                    return
            state["entered"] = True
            try:
                view._on_playback_tick()
            finally:
                state["entered"] = False

        view.playback_timer.timeout.connect(guarded_tick)

        def pump(remaining=200):
            if state["stop"]:
                return
            box = QtWidgets.QApplication.activeModalWidget()
            ready = isinstance(box, QtWidgets.QMessageBox) and (
                not require_modal_tick
                or state["modal_ticks"] >= 1
                or state["closed"] >= 1
            )
            if ready:
                box.done(QtWidgets.QMessageBox.StandardButton.Ok)
                state["closed"] += 1
            elif remaining <= 0:
                state["timeout"] = True
                if isinstance(box, QtWidgets.QMessageBox):
                    box.done(QtWidgets.QMessageBox.StandardButton.Ok)
                    state["closed"] += 1
                return
            QtCore.QTimer.singleShot(15, lambda: pump(remaining - 1))

        QtCore.QTimer.singleShot(0, pump)
        try:
            result = action()
        finally:
            state["stop"] = True
            try:
                view.playback_timer.timeout.disconnect(guarded_tick)
            except TypeError:
                pass
            view.playback_timer.timeout.connect(view._on_playback_tick)
        self.app.processEvents()
        self.assertFalse(
            state["timeout"],
            "save-error dialog was not closed by the playback timer",
        )
        self.assertGreaterEqual(state["closed"], 1)
        if require_modal_tick:
            self.assertGreaterEqual(state["modal_ticks"], 1)
        return result, state

    def _assert_memory_and_disk(self, view, frames, shape, before, label):
        self.assertIs(view.canvas.shapes[-1], shape)
        self.assertEqual(_labels(view), ["lesion", label])
        self.assertEqual(Path(view.filename), frames[0])
        self.assertEqual(Path(view.image_path), frames[0])
        self.assertEqual(self._snapshot(frames), before)
        anchor = json.loads(
            frames[0].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["label"] for item in anchor["shapes"]], ["lesion"]
        )

    def test_autosave_failure_keeps_dirty_and_blocks_frame_change(self):
        view, frames = self._open_sequence(auto_save=True)
        before = self._snapshot(frames)
        shape = self._add_memory_shape(view)
        self._lock(frames)
        calls_before = self._save_calls

        self._run_while_closing_save_errors(view, view.set_dirty)

        self.assertEqual(self._save_calls, calls_before + 1)
        self.assertTrue(view.dirty)
        self.assertTrue(view.actions.save.isEnabled())
        self._assert_memory_and_disk(view, frames, shape, before, "freshmark")

        questions = []

        def cancel_question(*_args, **_kwargs):
            questions.append("asked")
            return QtWidgets.QMessageBox.StandardButton.Cancel

        with patch.object(
            QtWidgets.QMessageBox, "question", side_effect=cancel_question
        ):
            view.open_next_image()
            self.app.processEvents()
            view.toggle_sequence_playback(True)
            self.app.processEvents()

        self.assertEqual(questions, ["asked", "asked"])
        self.assertEqual(Path(view.filename), frames[0])
        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)
        self._assert_memory_and_disk(view, frames, shape, before, "freshmark")

        self._unlock(frames)
        calls_before = self._save_calls
        view.set_dirty()
        self.assertEqual(self._save_calls, calls_before + 1)
        self.assertFalse(view.dirty)
        self.assertFalse(view.actions.save.isEnabled())
        saved = json.loads(
            frames[0].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["label"] for item in saved["shapes"]],
            ["lesion", "freshmark"],
        )
        self.assertEqual(saved["imagePath"], frames[0].name)
        self.assertEqual(saved["imageWidth"], 48)
        self.assertEqual(saved["imageHeight"], 32)
        neighbor_before = {
            path.name: path.with_suffix(".json").read_bytes()
            for path in frames[1:]
        }
        view.open_next_image()
        self.app.processEvents()
        self.assertEqual(Path(view.filename), frames[1])
        self.assertEqual(
            [item["label"] for item in saved["shapes"]],
            ["lesion", "freshmark"],
        )
        for path in frames[1:]:
            self.assertEqual(
                path.with_suffix(".json").read_bytes(),
                neighbor_before[path.name],
            )
        reloaded = json.loads(
            frames[0].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["label"] for item in reloaded["shapes"]],
            ["lesion", "freshmark"],
        )

    def test_autosave_failure_during_loop_survives_modal_timer(self):
        view, frames = self._open_sequence(auto_save=True)
        before = self._snapshot(frames)
        view.toggle_loop_playback(True)
        self.assertTrue(view.playback_timer.isActive())
        for _ in range(4):
            view._on_playback_tick()
        self.assertNotEqual(
            view._playback_display_index, view.fn_to_index[str(frames[0])]
        )
        shape = self._add_memory_shape(view, label="during-loop")
        self._lock(frames)
        calls_before = self._save_calls

        _result, state = self._run_while_closing_save_errors(
            view, view.set_dirty, require_modal_tick=True
        )

        self.assertEqual(self._save_calls, calls_before + 1)
        self.assertGreaterEqual(state["modal_ticks"], 1)
        self.assertTrue(view.dirty)
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertTrue(view.playback_timer.isActive())
        self._assert_memory_and_disk(
            view, frames, shape, before, "during-loop"
        )
        view._on_playback_tick()
        self._assert_memory_and_disk(
            view, frames, shape, before, "during-loop"
        )

        calls_before = self._save_calls
        self._run_while_closing_save_errors(
            view,
            lambda: view.toggle_loop_playback(False),
            require_modal_tick=True,
        )
        self.assertEqual(self._save_calls, calls_before + 1)
        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)
        self.assertTrue(view.dirty)
        self.assertEqual(view.canvas.pixmap.width(), 48)
        self._assert_memory_and_disk(
            view, frames, shape, before, "during-loop"
        )

    def test_autosave_failure_blocks_loop_start_until_save_succeeds(self):
        view, frames = self._open_sequence(auto_save=True)
        before = self._snapshot(frames)
        shape = self._add_memory_shape(view)
        self._lock(frames)
        self._run_while_closing_save_errors(view, view.set_dirty)
        self.assertTrue(view.dirty)

        recorded = []
        original = view._ensure_all_edits_saved

        def record_ensure():
            saved = original()
            recorded.append(saved)
            return saved

        view._ensure_all_edits_saved = record_ensure
        self._run_while_closing_save_errors(
            view, lambda: view.toggle_loop_playback(True)
        )

        self.assertEqual(recorded, [False])
        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)
        self.assertTrue(view.dirty)
        self._assert_memory_and_disk(view, frames, shape, before, "freshmark")

        self._unlock(frames)
        view._ensure_all_edits_saved = original
        view.toggle_loop_playback(True)
        self.assertTrue(view.playback_timer.isActive())
        self.assertFalse(view.dirty)
        saved = json.loads(
            frames[0].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["label"] for item in saved["shapes"]],
            ["lesion", "freshmark"],
        )
        for path in frames[1:]:
            self.assertEqual(
                json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))[
                    "shapes"
                ][0]["label"],
                "lesion",
            )
        view.toggle_loop_playback(False)
        self.assertEqual(
            [item.shape().label for item in view.label_list],
            ["lesion", "freshmark"],
        )

    def test_manual_save_failure_on_stop_and_start_keeps_memory(self):
        view, frames = self._open_sequence(auto_save=False)
        before = self._snapshot(frames)
        view.toggle_loop_playback(True)
        for _ in range(3):
            view._on_playback_tick()
        shape = self._add_memory_shape(view, label="manual-edit")
        view.set_dirty()
        self.assertTrue(view.dirty)
        self.assertEqual(self._snapshot(frames), before)
        self._lock(frames)

        questions = []

        def cancel_question(*_args, **_kwargs):
            questions.append("asked")
            return QtWidgets.QMessageBox.StandardButton.Cancel

        # Next asks before it stops. Cancel leaves the loop running and
        # does not write. The in-memory shape has to survive that.
        calls_before = self._save_calls
        preview = view._playback_display_index
        with patch.object(
            QtWidgets.QMessageBox, "question", side_effect=cancel_question
        ):
            view.open_next_image()
        self.assertEqual(questions, ["asked"])
        self.assertEqual(self._save_calls, calls_before)
        self.assertTrue(view.playback_timer.isActive())
        self.assertEqual(view._playback_mode, view.PLAYBACK_LOOP)
        self.assertEqual(view._playback_display_index, preview)
        self.assertIs(view.canvas.shapes[-1], shape)
        self.assertTrue(view.dirty)

        # An explicit stop still tries to save. The blocked label write
        # fails, and the shapes stay in memory instead of being reloaded.
        calls_before = self._save_calls
        self._run_while_closing_save_errors(
            view,
            lambda: view.toggle_loop_playback(False),
            require_modal_tick=True,
        )
        self.assertEqual(self._save_calls, calls_before + 1)
        self.assertFalse(view.playback_timer.isActive())
        self.assertIsNone(view._playback_mode)
        self.assertTrue(view.dirty)
        self.assertTrue(view.actions.save.isEnabled())
        self.assertEqual(view.canvas.pixmap.width(), 48)
        self._assert_memory_and_disk(
            view, frames, shape, before, "manual-edit"
        )

        recorded = []
        original = view._ensure_all_edits_saved

        def record_ensure():
            saved = original()
            recorded.append(saved)
            return saved

        view._ensure_all_edits_saved = record_ensure
        self._run_while_closing_save_errors(
            view, lambda: view.toggle_loop_playback(True)
        )
        self.assertEqual(recorded, [False])
        self.assertFalse(view.playback_timer.isActive())
        self._assert_memory_and_disk(
            view, frames, shape, before, "manual-edit"
        )

        self._unlock(frames)
        view._ensure_all_edits_saved = original
        calls_before = self._save_calls
        view.save_file()
        self.assertEqual(self._save_calls, calls_before + 1)
        self.assertFalse(view.dirty)
        self.assertFalse(view.actions.save.isEnabled())
        self.assertIs(view.canvas.shapes[-1], shape)
        saved = json.loads(
            frames[0].with_suffix(".json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["label"] for item in saved["shapes"]],
            ["lesion", "manual-edit"],
        )
        self.assertEqual(saved["imagePath"], frames[0].name)
        for path in frames[1:]:
            self.assertEqual(self._snapshot(frames)[path.name], before[path.name])
