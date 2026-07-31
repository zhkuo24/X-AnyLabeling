import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6 import QtCore, QtTest, QtWidgets

    from anylabeling.views.labeling.widgets.playback_control import (
        PlaybackControlBar,
        next_loop_index,
        symmetric_loop_bounds,
    )

    PYQT_AVAILABLE = True
except Exception:
    PYQT_AVAILABLE = False


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for playback helper tests"
)
class TestPlaybackLoopHelpers(unittest.TestCase):

    def test_symmetric_loop_bounds_center_and_edges(self):
        self.assertEqual(symmetric_loop_bounds(5, 10, 2), (3, 7))
        self.assertEqual(symmetric_loop_bounds(0, 10, 3), (0, 3))
        self.assertEqual(symmetric_loop_bounds(9, 10, 3), (6, 9))

    def test_symmetric_loop_bounds_clamps_inputs_and_handles_empty(self):
        self.assertEqual(symmetric_loop_bounds(-5, 10, 2), (0, 2))
        self.assertEqual(symmetric_loop_bounds(50, 10, 2), (7, 9))
        self.assertEqual(symmetric_loop_bounds(4, 10, -2), (4, 4))
        self.assertEqual(symmetric_loop_bounds(4, 0, 2), (0, -1))

    def test_next_loop_index_advances_wraps_and_recovers(self):
        self.assertEqual(next_loop_index(2, 2, 4), 3)
        self.assertEqual(next_loop_index(4, 2, 4), 2)
        self.assertEqual(next_loop_index(99, 2, 4), 2)
        self.assertEqual(next_loop_index(-1, 2, 4), 2)

    def test_next_loop_index_rejects_invalid_bounds(self):
        with self.assertRaises(ValueError):
            next_loop_index(2, 4, 3)


@unittest.skipUnless(
    PYQT_AVAILABLE, "PyQt6 is required for playback control tests"
)
class TestPlaybackControl(unittest.TestCase):

    def setUp(self):
        self.app = QtWidgets.QApplication.instance()
        if self.app is None:
            self.app = QtWidgets.QApplication([])
        self.widget = PlaybackControlBar()
        self.widget.show()
        self.app.processEvents()

    def tearDown(self):
        self.widget.close()
        self.app.processEvents()

    def _groove_point_for_value(self, value):
        slider = self.widget.progress_slider
        option = QtWidgets.QStyleOptionSlider()
        slider.initStyleOption(option)
        style = slider.style()
        groove = style.subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            option,
            QtWidgets.QStyle.SubControl.SC_SliderGroove,
            slider,
        )
        handle = style.subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            option,
            QtWidgets.QStyle.SubControl.SC_SliderHandle,
            slider,
        )
        span = max(0, groove.width() - handle.width())
        position = QtWidgets.QStyle.sliderPositionFromValue(
            slider.minimum(),
            slider.maximum(),
            value,
            span,
            option.upsideDown,
        )
        return (
            QtCore.QPoint(
                groove.left() + position + handle.width() // 2,
                groove.center().y(),
            ),
            option,
        )

    def test_defaults_and_zero_frame_state(self):
        self.assertEqual(self.widget.total_frames, 0)
        self.assertEqual(self.widget.current_frame, 0)
        self.assertEqual(self.widget.frame_label.text(), "0 / 0")
        self.assertEqual(self.widget.progress_slider.minimum(), 0)
        self.assertEqual(self.widget.progress_slider.maximum(), 0)
        self.assertFalse(self.widget.prev_button.isEnabled())
        self.assertFalse(self.widget.play_button.isEnabled())
        self.assertFalse(self.widget.next_button.isEnabled())
        self.assertFalse(self.widget.loop_button.isEnabled())
        self.assertFalse(self.widget.progress_slider.isEnabled())
        self.assertEqual(
            (
                self.widget.loop_radius_spinbox.minimum(),
                self.widget.loop_radius_spinbox.maximum(),
            ),
            (0, 999),
        )
        self.assertEqual(
            (
                self.widget.fps_spinbox.minimum(),
                self.widget.fps_spinbox.maximum(),
            ),
            (1, 120),
        )

    def test_current_frame_clamps_and_updates_navigation(self):
        self.widget.set_total_frames(5)

        self.widget.set_current_frame(2)
        self.assertEqual(self.widget.current_frame, 2)
        self.assertEqual(self.widget.progress_slider.value(), 2)
        self.assertEqual(self.widget.frame_label.text(), "3 / 5")
        self.assertTrue(self.widget.prev_button.isEnabled())
        self.assertTrue(self.widget.next_button.isEnabled())

        self.widget.set_current_frame(-100)
        self.assertEqual(self.widget.current_frame, 0)
        self.assertFalse(self.widget.prev_button.isEnabled())
        self.assertTrue(self.widget.next_button.isEnabled())

        self.widget.set_current_frame(100)
        self.assertEqual(self.widget.current_frame, 4)
        self.assertTrue(self.widget.prev_button.isEnabled())
        self.assertFalse(self.widget.next_button.isEnabled())

    def test_one_frame_disables_non_actionable_playback_controls(self):
        self.widget.set_total_frames(1)

        self.assertEqual(self.widget.frame_label.text(), "1 / 1")
        self.assertFalse(self.widget.prev_button.isEnabled())
        self.assertFalse(self.widget.next_button.isEnabled())
        self.assertFalse(self.widget.play_button.isEnabled())
        self.assertFalse(self.widget.loop_button.isEnabled())
        self.assertFalse(self.widget.progress_slider.isEnabled())

    def test_step_buttons_emit_direction(self):
        self.widget.set_total_frames(3)
        self.widget.set_current_frame(1)
        spy = QtTest.QSignalSpy(self.widget.step_requested)

        self.widget.prev_button.click()
        self.widget.next_button.click()

        self.assertEqual(len(spy), 2)
        self.assertEqual(list(spy[0]), [-1])
        self.assertEqual(list(spy[1]), [1])

    def test_play_and_loop_requests_and_programmatic_states(self):
        self.widget.set_total_frames(3)
        play_spy = QtTest.QSignalSpy(self.widget.play_requested)
        state_spy = QtTest.QSignalSpy(
            self.widget.playback_state_requested
        )
        loop_spy = QtTest.QSignalSpy(self.widget.loop_requested)

        self.widget.play_button.click()
        self.widget.loop_button.click()
        self.assertEqual(len(play_spy), 1)
        self.assertEqual(list(state_spy[0]), [True])
        self.assertEqual(len(loop_spy), 1)
        self.assertTrue(self.widget.play_button.isChecked())
        self.assertTrue(self.widget.loop_button.isChecked())

        self.widget.set_playback_state(False)
        self.widget.set_loop_state(False)
        self.assertFalse(self.widget.play_button.isChecked())
        self.assertFalse(self.widget.loop_button.isChecked())
        self.assertEqual(len(play_spy), 1)
        self.assertEqual(len(state_spy), 1)
        self.assertEqual(len(loop_spy), 1)

    def test_main_play_button_requests_pause_while_loop_is_active(self):
        self.widget.set_total_frames(3)
        self.widget.set_playback_state(True)
        self.widget.set_loop_state(True)
        play_spy = QtTest.QSignalSpy(self.widget.play_requested)
        state_spy = QtTest.QSignalSpy(
            self.widget.playback_state_requested
        )

        self.assertTrue(self.widget.play_button.isChecked())
        self.assertEqual(self.widget.play_button.toolTip(), "Pause")
        self.widget.play_button.click()

        self.assertEqual(len(play_spy), 1)
        self.assertEqual(list(play_spy[0]), [])
        self.assertEqual(list(state_spy[0]), [False])
        self.assertFalse(self.widget.play_button.isChecked())
        self.assertTrue(self.widget.loop_button.isChecked())

    def test_pause_is_requested_on_press_before_mouse_release(self):
        self.widget.set_total_frames(3)
        self.widget.set_playback_state(True)
        state_spy = QtTest.QSignalSpy(
            self.widget.playback_state_requested
        )
        center = self.widget.play_button.rect().center()

        QtTest.QTest.mousePress(
            self.widget.play_button,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            center,
        )
        self.app.processEvents()

        self.assertEqual(len(state_spy), 1)
        self.assertEqual(list(state_spy[0]), [False])
        self.assertFalse(self.widget.play_button.isChecked())

        QtTest.QTest.mouseRelease(
            self.widget.play_button,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            center,
        )
        self.app.processEvents()

        self.assertEqual(len(state_spy), 1)
        self.assertFalse(self.widget.play_button.isChecked())

    def test_rapid_second_click_after_pause_cannot_restart_playback(self):
        self.widget.set_total_frames(3)
        self.widget.set_playback_state(True)
        state_spy = QtTest.QSignalSpy(
            self.widget.playback_state_requested
        )

        self.widget.play_button.click()
        self.widget.play_button.click()

        self.assertEqual(
            [list(request) for request in state_spy],
            [[False], [False]],
        )
        self.assertFalse(self.widget.play_button.isChecked())
        self.assertFalse(self.widget._is_playing)

    def test_programmatic_slider_updates_do_not_request_seek(self):
        self.widget.set_total_frames(10)
        seek_spy = QtTest.QSignalSpy(self.widget.seek_requested)

        self.widget.set_current_frame(5)
        self.assertEqual(len(seek_spy), 0)

        self.widget.progress_slider.setValue(6)
        self.assertEqual(len(seek_spy), 1)
        self.assertEqual(list(seek_spy[0]), [6])
        self.assertEqual(self.widget.frame_label.text(), "7 / 10")

        self.widget.set_total_frames(4)
        self.assertEqual(self.widget.current_frame, 3)
        self.assertEqual(len(seek_spy), 1)

    def test_slider_drag_previews_then_requests_one_seek_on_release(self):
        self.widget.set_total_frames(10)
        seek_spy = QtTest.QSignalSpy(self.widget.seek_requested)

        self.widget.progress_slider.sliderPressed.emit()
        self.widget.progress_slider.setValue(4)
        self.widget.progress_slider.setValue(7)

        self.assertEqual(len(seek_spy), 0)
        self.assertEqual(self.widget.frame_label.text(), "8 / 10")

        self.widget.progress_slider.sliderReleased.emit()

        self.assertEqual(len(seek_spy), 1)
        self.assertEqual(list(seek_spy[0]), [7])

    def test_slider_groove_click_seeks_to_absolute_position(self):
        self.widget.resize(900, self.widget.sizeHint().height())
        self.widget.set_total_frames(101)
        self.app.processEvents()
        seek_spy = QtTest.QSignalSpy(self.widget.seek_requested)
        click_position, _option = self._groove_point_for_value(73)

        QtTest.QTest.mouseClick(
            self.widget.progress_slider,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            click_position,
        )
        self.app.processEvents()

        self.assertEqual(self.widget.progress_slider.value(), 73)
        self.assertEqual(self.widget.current_frame, 73)
        self.assertEqual(len(seek_spy), 1)
        self.assertEqual(list(seek_spy[0]), [73])

    def test_slider_groove_click_honors_right_to_left_direction(self):
        self.widget.resize(900, self.widget.sizeHint().height())
        self.widget.progress_slider.setLayoutDirection(
            QtCore.Qt.LayoutDirection.RightToLeft
        )
        self.widget.set_total_frames(101)
        self.app.processEvents()
        seek_spy = QtTest.QSignalSpy(self.widget.seek_requested)
        click_position, option = self._groove_point_for_value(27)

        self.assertTrue(option.upsideDown)
        QtTest.QTest.mouseClick(
            self.widget.progress_slider,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            click_position,
        )
        self.app.processEvents()

        self.assertEqual(self.widget.progress_slider.value(), 27)
        self.assertEqual(self.widget.current_frame, 27)
        self.assertEqual(len(seek_spy), 1)
        self.assertEqual(list(seek_spy[0]), [27])

    def test_playback_sync_does_not_fight_active_slider_drag(self):
        self.widget.set_total_frames(10)
        seek_spy = QtTest.QSignalSpy(self.widget.seek_requested)

        self.widget.progress_slider.sliderPressed.emit()
        self.widget.progress_slider.setValue(7)
        self.widget.set_total_frames(10)
        self.widget.set_current_frame(3)

        self.assertEqual(self.widget.progress_slider.value(), 7)
        self.assertEqual(self.widget.frame_label.text(), "8 / 10")
        self.assertEqual(len(seek_spy), 0)

        self.widget.progress_slider.sliderReleased.emit()
        self.assertEqual(list(seek_spy[0]), [7])

    def test_fps_setter_clamps_without_emitting(self):
        spy = QtTest.QSignalSpy(self.widget.fps_changed)

        self.widget.set_fps(1000)
        self.assertEqual(self.widget.fps_spinbox.value(), 120)
        self.assertEqual(len(spy), 0)

        self.widget.set_fps(-10)
        self.assertEqual(self.widget.fps_spinbox.value(), 1)
        self.assertEqual(len(spy), 0)

        self.widget.fps_spinbox.setValue(42)
        self.assertEqual(len(spy), 1)
        self.assertEqual(list(spy[0]), [42])

    def test_loop_radius_signal_uses_spinbox_limits(self):
        spy = QtTest.QSignalSpy(self.widget.loop_radius_changed)

        self.widget.loop_radius_spinbox.setValue(1000)

        self.assertEqual(self.widget.loop_radius_spinbox.value(), 999)
        self.assertEqual(len(spy), 1)
        self.assertEqual(list(spy[0]), [999])

    def test_zero_frames_clear_active_states_without_requests(self):
        self.widget.set_total_frames(4)
        self.widget.set_playback_state(True)
        self.widget.set_loop_state(True)
        play_spy = QtTest.QSignalSpy(self.widget.play_requested)
        loop_spy = QtTest.QSignalSpy(self.widget.loop_requested)

        self.widget.set_total_frames(0)

        self.assertFalse(self.widget.play_button.isChecked())
        self.assertFalse(self.widget.loop_button.isChecked())
        self.assertEqual(self.widget.frame_label.text(), "0 / 0")
        self.assertEqual(len(play_spy), 0)
        self.assertEqual(len(loop_spy), 0)


if __name__ == "__main__":
    unittest.main()
