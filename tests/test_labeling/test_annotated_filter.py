import atexit
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_SETTINGS_ROOT = tempfile.mkdtemp(prefix="xany-filter-test-settings-")
os.environ["XDG_CONFIG_HOME"] = _SETTINGS_ROOT
atexit.register(shutil.rmtree, _SETTINGS_ROOT, ignore_errors=True)

try:
    from PyQt6 import QtGui, QtTest, QtWidgets

    from anylabeling import config as anylabeling_config
    from anylabeling.config import get_config, set_work_directory
    from anylabeling.resources import resources  # noqa: F401
    from anylabeling.views.labeling.label_widget import (
        LabelingWidget,
        _select_filtered_file,
    )
    from anylabeling.views.mainwindow import MainWindow

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False


@unittest.skipUnless(PYQT_AVAILABLE, "PyQt6 is required for filter tests")
class TestFilteredFileSelection(unittest.TestCase):

    def test_preserves_current_or_selects_nearest_next_then_previous(self):
        all_files = ["0", "1", "2", "3", "4", "5", "6"]
        filtered = ["0", "2", "3", "6"]

        self.assertEqual(
            _select_filtered_file(all_files, filtered, "2"),
            "2",
        )
        self.assertEqual(
            _select_filtered_file(all_files, filtered, "1"),
            "2",
        )
        self.assertEqual(
            _select_filtered_file(all_files, filtered, "5"),
            "6",
        )
        self.assertEqual(
            _select_filtered_file(all_files, filtered, "missing"),
            "0",
        )
        self.assertEqual(
            _select_filtered_file(all_files, ["0", "2"], "6"),
            "2",
        )
        self.assertIsNone(_select_filtered_file(all_files, [], "2"))

    def test_label_path_honors_output_directory(self):
        image = "/images/case/frame.png"
        source_widget = SimpleNamespace(output_dir=None)
        output_widget = SimpleNamespace(output_dir="/labels")

        self.assertEqual(
            LabelingWidget._label_file_for_image(source_widget, image),
            "/images/case/frame.json",
        )
        self.assertEqual(
            LabelingWidget._label_file_for_image(output_widget, image),
            "/labels/frame.json",
        )


@unittest.skipUnless(PYQT_AVAILABLE, "PyQt6 is required for filter tests")
class TestAnnotatedOnlyFilterIntegration(unittest.TestCase):

    @staticmethod
    def _write_label(label_path, image_path):
        label_path.write_text(
            json.dumps(
                {
                    "version": "5.0.0",
                    "flags": {},
                    "shapes": [],
                    "imagePath": Path(image_path).name,
                    "imageData": None,
                    "imageHeight": 32,
                    "imageWidth": 32,
                }
            ),
            encoding="utf-8",
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="xany-annotated-filter-"
        )
        root = Path(self.temp_dir.name)
        self.frame_dir = root / "frames"
        self.frame_dir.mkdir()
        self.frames = []
        annotated_indexes = {0, 2, 3, 6}
        for index in range(7):
            name = (
                f"{index:03d}_keep.png"
                if index in {0, 1, 2, 4}
                else f"{index:03d}_drop.png"
            )
            image_path = self.frame_dir / name
            image = QtGui.QImage(
                32,
                32,
                QtGui.QImage.Format.Format_RGB32,
            )
            image.fill(QtGui.QColor(index * 20, 40, 100))
            self.assertTrue(image.save(str(image_path)))
            self.frames.append(str(image_path))
            if index in annotated_indexes:
                self._write_label(
                    image_path.with_suffix(".json"),
                    image_path,
                )

        set_work_directory(str(root / "settings"))
        anylabeling_config.current_config_file = "{}"
        config = get_config("{}")
        config["auto_save"] = False
        config["keep_prev"] = False
        config["exif_scan_enabled"] = False

        self.app = QtWidgets.QApplication.instance()
        if self.app is None:
            self.app = QtWidgets.QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self.window = MainWindow(
            self.app,
            config=config,
            filename=str(self.frame_dir),
        )
        self.window.resize(1100, 760)
        self.window.show()
        self.view = self.window.labeling_widget.view
        self.assertTrue(
            self._wait_until(
                lambda: len(self.view.image_list) == 7
                and self.view.image_path is not None
            )
        )

    def tearDown(self):
        self.window.hide()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def _wait_until(self, predicate, timeout_ms=5000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            QtTest.QTest.qWait(10)
        self.app.processEvents()
        return bool(predicate())

    def _select_frame(self, filename):
        self.view.file_list_widget.setCurrentRow(
            self.view.fn_to_index[filename]
        )
        self.assertTrue(
            self._wait_until(lambda: str(self.view.filename) == filename)
        )

    def test_filter_search_navigation_and_playback_stay_consistent(self):
        self._select_frame(self.frames[1])

        self.view.annotated_only_checkbox.setChecked(True)
        self.assertTrue(
            self._wait_until(lambda: len(self.view.image_list) == 4)
        )
        self.assertEqual(
            self.view.image_list,
            [self.frames[index] for index in (0, 2, 3, 6)],
        )
        self.assertEqual(self.view.filename, self.frames[2])
        self.assertEqual(self.view.image_path, self.frames[2])
        self.assertEqual(
            self.view.fn_to_index,
            {
                self.frames[0]: 0,
                self.frames[2]: 1,
                self.frames[3]: 2,
                self.frames[6]: 3,
            },
        )
        self.assertEqual(self.view.playback_control.total_frames, 4)
        self.assertEqual(self.view.playback_control.current_frame, 1)
        self.assertEqual(self.view.playback_control.frame_label.text(), "2 / 4")
        self.assertEqual(
            self.view.playback_control.progress_slider.maximum(),
            3,
        )
        self.assertEqual(self.view.playback_control.progress_slider.value(), 1)

        self.view.open_next_image()
        self.assertEqual(self.view.filename, self.frames[3])

        self.view.annotated_only_checkbox.setChecked(False)
        self.assertTrue(
            self._wait_until(lambda: len(self.view.image_list) == 7)
        )
        self.assertEqual(self.view.filename, self.frames[3])
        self.assertEqual(self.view.image_path, self.frames[3])

        self.view.file_search.setText("keep")
        self.view.file_search_changed()
        self.assertEqual(
            self.view.image_list,
            [self.frames[index] for index in (0, 1, 2, 4)],
        )
        self.assertEqual(self.view.filename, self.frames[4])

        self.view.annotated_only_checkbox.setChecked(True)
        self.assertEqual(
            self.view.image_list,
            [self.frames[index] for index in (0, 2)],
        )
        self.assertEqual(self.view.filename, self.frames[2])

        self.view.file_search.setText("does-not-exist")
        self.view.file_search_changed()
        self.assertEqual(self.view.image_list, [])
        self.assertEqual(self.view.fn_to_index, {})
        self.assertIsNone(self.view.filename)
        self.assertIsNone(self.view.image_path)
        self.assertEqual(self.view.playback_control.total_frames, 0)
        self.assertFalse(self.view.playback_control.play_button.isEnabled())

        self.view.file_search.clear()
        self.view.file_search_changed()
        self.assertEqual(
            self.view.image_list,
            [self.frames[index] for index in (0, 2, 3, 6)],
        )

        self.view.annotated_only_checkbox.setChecked(False)
        self._select_frame(self.frames[1])
        self.view.toggle_loop_playback()
        self.assertTrue(self.view.playback_timer.isActive())

        self.view.annotated_only_checkbox.setChecked(True)

        self.assertFalse(self.view.playback_timer.isActive())
        self.assertIsNone(self.view._playback_mode)
        self.assertEqual(
            self.view.image_list,
            [self.frames[index] for index in (0, 2, 3, 6)],
        )
        self.assertEqual(self.view.filename, self.frames[2])

    def test_filter_uses_output_directory_and_keeps_source_image_path(self):
        output_dir = Path(self.temp_dir.name) / "labels"
        output_dir.mkdir()
        for index in (1, 5):
            image_path = Path(self.frames[index])
            self._write_label(
                output_dir / f"{image_path.stem}.json",
                image_path,
            )

        self.view.output_dir = str(output_dir)
        self.assertTrue(
            self.view.import_image_folder(
                str(self.frame_dir),
                load=True,
                preserve_current=True,
                force_reload=True,
            )
        )
        self.view.annotated_only_checkbox.setChecked(True)

        self.assertEqual(
            self.view.image_list,
            [self.frames[index] for index in (1, 5)],
        )
        self.assertEqual(self.view.filename, self.frames[1])
        self.assertEqual(self.view.image_path, self.frames[1])
        self.assertTrue(self.view._playback_available())

        self.view.toggle_loop_playback()
        self.assertTrue(self.view.playback_timer.isActive())
        self.view.toggle_loop_playback()
        self.assertFalse(self.view.playback_timer.isActive())

    def test_filter_reload_discards_unsaved_changes_when_confirmed(self):
        self.assertEqual(self.view.filename, self.frames[0])
        self.view.shape_text_edit.setPlainText("unsaved description")
        self.assertTrue(self.view.dirty)

        with mock.patch.object(
            self.view,
            "may_continue",
            return_value=True,
        ):
            self.view.annotated_only_checkbox.setChecked(True)

        self.assertEqual(self.view.filename, self.frames[0])
        self.assertEqual(self.view.image_path, self.frames[0])
        self.assertEqual(self.view.shape_text_edit.toPlainText(), "")
        self.assertFalse(self.view.dirty)

    def test_cancelled_output_directory_change_rolls_back(self):
        output_dir = Path(self.temp_dir.name) / "new-labels"
        output_dir.mkdir()
        original_files = list(self.view.image_list)
        original_filename = self.view.filename

        with (
            mock.patch.object(
                QtWidgets.QFileDialog,
                "getExistingDirectory",
                return_value=str(output_dir),
            ),
            mock.patch.object(
                self.view,
                "may_continue",
                return_value=False,
            ),
        ):
            self.view.change_output_dir_dialog()

        self.assertIsNone(self.view.output_dir)
        self.assertEqual(self.view.image_list, original_files)
        self.assertEqual(self.view.filename, original_filename)
        self.assertEqual(self.view.image_path, original_filename)

    def test_recent_file_leaves_filtered_sequence_safely(self):
        self.view.annotated_only_checkbox.setChecked(True)
        self.assertEqual(len(self.view.image_list), 4)

        self.view.load_recent(self.frames[1])

        self.assertEqual(self.view.image_list, [])
        self.assertEqual(self.view.fn_to_index, {})
        self.assertFalse(self.view.annotated_only_checkbox.isChecked())
        self.assertFalse(self.view.annotated_only_checkbox.isEnabled())
        self.assertEqual(self.view.filename, self.frames[1])
        self.assertEqual(self.view.image_path, self.frames[1])
        self.assertEqual(self.view.playback_control.total_frames, 0)
        self.assertFalse(self.view._playback_available())

    def test_delete_refreshes_active_attribute_search(self):
        label_path = Path(self.frames[0]).with_suffix(".json")
        label_data = json.loads(label_path.read_text(encoding="utf-8"))
        label_data["shapes"] = [
            {
                "label": "target",
                "points": [[2, 2], [20, 2], [20, 20]],
                "group_id": None,
                "description": "",
                "difficult": False,
                "shape_type": "polygon",
                "flags": {},
                "attributes": {},
            }
        ]
        label_path.write_text(json.dumps(label_data), encoding="utf-8")
        self.assertTrue(
            self.view.import_image_folder(
                str(self.frame_dir),
                load=True,
                preserve_current=True,
                force_reload=True,
            )
        )
        self.view.file_search.setText("shape::1")
        self.view.file_search_changed()
        self.assertEqual(self.view.image_list, [self.frames[0]])

        with mock.patch.object(
            QtWidgets.QMessageBox,
            "warning",
            return_value=QtWidgets.QMessageBox.StandardButton.Yes,
        ):
            self.view.delete_file()

        self.assertFalse(label_path.exists())
        self.assertEqual(self.view.image_list, [])
        self.assertEqual(self.view.fn_to_index, {})
        self.assertIsNone(self.view.filename)
        self.assertIsNone(self.view.image_path)


if __name__ == "__main__":
    unittest.main()
