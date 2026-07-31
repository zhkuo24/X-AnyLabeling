import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6 import QtWidgets

    from anylabeling.views.labeling.label_widget import LabelingWidget
    from anylabeling.views.labeling.widgets.canvas import Canvas

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for media playback tests"
)
class TestMediaPlaybackState(unittest.TestCase):

    def test_loop_marks_main_pause_and_loop_controls_as_active(self):
        playback_control = SimpleNamespace(
            set_total_frames=Mock(),
            set_current_frame=Mock(),
            set_playback_state=Mock(),
            set_loop_state=Mock(),
        )
        widget = SimpleNamespace(
            image_list=["0.png", "1.png"],
            playback_control=playback_control,
            playback_timer=SimpleNamespace(isActive=lambda: True),
            _playback_mode=LabelingWidget.PLAYBACK_LOOP,
            PLAYBACK_SEQUENCE=LabelingWidget.PLAYBACK_SEQUENCE,
            PLAYBACK_LOOP=LabelingWidget.PLAYBACK_LOOP,
        )

        LabelingWidget._sync_playback_control(widget, frame_index=0)

        playback_control.set_playback_state.assert_called_once_with(True)
        playback_control.set_loop_state.assert_called_once_with(True)

    def test_main_play_button_stops_loop_without_starting_sequence(self):
        widget = SimpleNamespace(
            _playback_mode=LabelingWidget.PLAYBACK_LOOP,
            playback_timer=SimpleNamespace(isActive=lambda: True),
            PLAYBACK_SEQUENCE=LabelingWidget.PLAYBACK_SEQUENCE,
            PLAYBACK_LOOP=LabelingWidget.PLAYBACK_LOOP,
            _stop_playback=Mock(),
            _playback_available=Mock(),
        )

        LabelingWidget.toggle_sequence_playback(widget)

        widget._stop_playback.assert_called_once_with()
        widget._playback_available.assert_not_called()

    def test_explicit_pause_stops_an_inconsistent_active_timer(self):
        widget = SimpleNamespace(
            _playback_mode=None,
            playback_timer=SimpleNamespace(isActive=lambda: True),
            _stop_playback=Mock(),
            _sync_playback_control=Mock(),
        )

        LabelingWidget.toggle_sequence_playback(widget, False)

        widget._stop_playback.assert_called_once_with()
        widget._sync_playback_control.assert_not_called()

    def test_loop_label_selection_enters_edit_mode_and_updates_contour(self):
        state = {"editing": False}
        selected_shape = object()
        canvas = SimpleNamespace(
            is_brush_mode=False,
            current=None,
            editing=lambda: state["editing"],
            select_shapes=Mock(),
            deselect_shape=Mock(),
        )
        set_edit_mode = Mock(
            side_effect=lambda: state.__setitem__("editing", True)
        )
        item = SimpleNamespace(shape=lambda: selected_shape)
        widget = SimpleNamespace(
            _no_selection_slot=False,
            _playback_mode=LabelingWidget.PLAYBACK_LOOP,
            PLAYBACK_LOOP=LabelingWidget.PLAYBACK_LOOP,
            canvas=canvas,
            set_edit_mode=set_edit_mode,
            label_list=SimpleNamespace(selected_items=lambda: [item]),
        )

        LabelingWidget.label_selection_changed(widget)

        set_edit_mode.assert_called_once_with()
        canvas.select_shapes.assert_called_once_with([selected_shape])

    def test_sequence_stop_reenables_canvas(self):
        canvas = SimpleNamespace(setEnabled=Mock())
        widget = SimpleNamespace(
            _playback_mode=LabelingWidget.PLAYBACK_SEQUENCE,
            PLAYBACK_SEQUENCE=LabelingWidget.PLAYBACK_SEQUENCE,
            PLAYBACK_LOOP=LabelingWidget.PLAYBACK_LOOP,
            _playback_anchor_index=None,
            _playback_display_index=1,
            _playback_preview_cache={},
            _playback_anchor_pixmap=None,
            playback_timer=SimpleNamespace(stop=Mock()),
            canvas=canvas,
            filename="frame.png",
            _sync_playback_control=Mock(),
        )

        LabelingWidget._stop_playback(widget)

        canvas.setEnabled.assert_called_once_with(True)
        self.assertIsNone(widget._playback_mode)

    def test_sequence_pauses_before_a_modal_dialog_can_change_frames(self):
        widget = SimpleNamespace(
            _playback_mode=LabelingWidget.PLAYBACK_SEQUENCE,
            PLAYBACK_SEQUENCE=LabelingWidget.PLAYBACK_SEQUENCE,
            PLAYBACK_LOOP=LabelingWidget.PLAYBACK_LOOP,
            _stop_playback=Mock(),
            status=Mock(),
            tr=lambda text: text,
        )

        with patch.object(
            QtWidgets.QApplication,
            "activeModalWidget",
            return_value=object(),
        ):
            LabelingWidget._on_playback_tick(widget)

        widget._stop_playback.assert_called_once_with()
        widget.status.assert_called_once()

    def test_loop_pauses_before_a_modal_dialog_blocks_controls(self):
        widget = SimpleNamespace(
            _playback_mode=LabelingWidget.PLAYBACK_LOOP,
            PLAYBACK_SEQUENCE=LabelingWidget.PLAYBACK_SEQUENCE,
            PLAYBACK_LOOP=LabelingWidget.PLAYBACK_LOOP,
            _stop_playback=Mock(),
            status=Mock(),
            tr=lambda text: text,
        )

        with patch.object(
            QtWidgets.QApplication,
            "activeModalWidget",
            return_value=object(),
        ):
            LabelingWidget._on_playback_tick(widget)

        widget._stop_playback.assert_called_once_with()
        widget.status.assert_called_once()

    def test_playback_pauses_before_a_popup_menu_grabs_clicks(self):
        widget = SimpleNamespace(
            _playback_mode=LabelingWidget.PLAYBACK_LOOP,
            PLAYBACK_SEQUENCE=LabelingWidget.PLAYBACK_SEQUENCE,
            PLAYBACK_LOOP=LabelingWidget.PLAYBACK_LOOP,
            _stop_playback=Mock(),
            status=Mock(),
            tr=lambda text: text,
        )

        with (
            patch.object(
                QtWidgets.QApplication,
                "activeModalWidget",
                return_value=None,
            ),
            patch.object(
                QtWidgets.QApplication,
                "activePopupWidget",
                return_value=object(),
            ),
        ):
            LabelingWidget._on_playback_tick(widget)

        widget._stop_playback.assert_called_once_with()
        widget.status.assert_called_once()

    def test_preview_pixmap_preserves_annotation_state(self):
        shapes = [object()]
        current = object()
        canvas = SimpleNamespace(
            pixmap=object(),
            shapes=shapes,
            current=current,
            is_brush_mode=True,
            update=Mock(),
        )
        preview = object()

        Canvas.set_preview_pixmap(canvas, preview)

        self.assertIs(canvas.pixmap, preview)
        self.assertIs(canvas.shapes, shapes)
        self.assertIs(canvas.current, current)
        self.assertTrue(canvas.is_brush_mode)
        canvas.update.assert_called_once_with()


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for dirty-state tests"
)
class TestPlaybackDirtyState(unittest.TestCase):

    def test_generic_discard_stays_dirty_until_document_changes(self):
        widget = SimpleNamespace(
            dirty=True,
            filename="frame.png",
            tr=lambda text: text,
            set_clean=Mock(),
        )

        with patch.object(
            QtWidgets.QMessageBox,
            "question",
            return_value=QtWidgets.QMessageBox.StandardButton.Discard,
        ):
            result = LabelingWidget.may_continue(widget)

        self.assertTrue(result)
        self.assertTrue(widget.dirty)
        widget.set_clean.assert_not_called()

    def test_playback_discard_restores_saved_anchor_before_starting(self):
        widget = SimpleNamespace(
            dirty=True,
            filename="frame.png",
            _config={"keep_prev": True},
            may_continue=Mock(return_value=True),
            set_clean=Mock(),
            load_file=Mock(return_value=True),
        )
        widget.set_clean.side_effect = lambda: setattr(widget, "dirty", False)

        result = LabelingWidget._confirm_playback_frame_change(widget)

        self.assertTrue(result)
        widget.load_file.assert_called_once_with("frame.png")
        self.assertTrue(widget._config["keep_prev"])

    def test_save_failure_or_cancel_does_not_allow_frame_change(self):
        widget = SimpleNamespace(
            dirty=True,
            filename="frame.png",
            tr=lambda text: text,
            save_file=Mock(),
        )

        with patch.object(
            QtWidgets.QMessageBox,
            "question",
            return_value=QtWidgets.QMessageBox.StandardButton.Save,
        ):
            result = LabelingWidget.may_continue(widget)

        self.assertFalse(result)
        self.assertTrue(widget.dirty)

    def test_successful_save_allows_frame_change(self):
        widget = SimpleNamespace(
            dirty=True,
            filename="frame.png",
            tr=lambda text: text,
        )
        widget.save_file = lambda: setattr(widget, "dirty", False)

        with patch.object(
            QtWidgets.QMessageBox,
            "question",
            return_value=QtWidgets.QMessageBox.StandardButton.Save,
        ):
            result = LabelingWidget.may_continue(widget)

        self.assertTrue(result)
        self.assertFalse(widget.dirty)


if __name__ == "__main__":
    unittest.main()
