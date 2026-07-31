from PyQt6 import QtCore, QtWidgets


def symmetric_loop_bounds(anchor, total, radius):
    """Return inclusive bounds for a symmetric loop around ``anchor``.

    The anchor and radius are clamped to values valid for the sequence.  An
    empty sequence is represented by ``(0, -1)``.
    """

    total = max(0, int(total))
    if total == 0:
        return 0, -1

    anchor = min(max(0, int(anchor)), total - 1)
    radius = max(0, int(radius))
    return max(0, anchor - radius), min(total - 1, anchor + radius)


def next_loop_index(current, start, end):
    """Return the next index in the inclusive range ``[start, end]``."""

    current = int(current)
    start = int(start)
    end = int(end)
    if end < start:
        raise ValueError(
            "loop end must be greater than or equal to loop start"
        )
    if current < start or current >= end:
        return start
    return current + 1


class AbsoluteSeekSlider(QtWidgets.QSlider):
    """A slider whose groove accepts absolute-position left clicks.

    ``QSlider`` normally applies a style-dependent page step when its groove is
    clicked.  A playback timeline is more useful when that click seeks to the
    corresponding position directly.  The conversion below uses the active
    style's groove and handle geometry, including its ``upsideDown`` flag, so
    right-to-left and inverted appearances keep their native direction.
    """

    def mousePressEvent(self, event):
        if (
            event.button() != QtCore.Qt.MouseButton.LeftButton
            or not self.isEnabled()
        ):
            super().mousePressEvent(event)
            return

        option = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            option,
            QtWidgets.QStyle.SubControl.SC_SliderGroove,
            self,
        )
        handle = style.subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            option,
            QtWidgets.QStyle.SubControl.SC_SliderHandle,
            self,
        )
        position = event.position().toPoint()

        # Preserve QSlider's native press-and-drag behavior on the handle.
        if handle.contains(position) or not groove.contains(position):
            super().mousePressEvent(event)
            return

        if self.orientation() == QtCore.Qt.Orientation.Horizontal:
            slider_length = handle.width()
            slider_min = groove.left()
            slider_max = groove.right() - slider_length + 1
            pixel_position = position.x() - slider_length // 2
        else:
            slider_length = handle.height()
            slider_min = groove.top()
            slider_max = groove.bottom() - slider_length + 1
            pixel_position = position.y() - slider_length // 2

        span = max(0, slider_max - slider_min)
        value = QtWidgets.QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            pixel_position - slider_min,
            span,
            option.upsideDown,
        )
        self.setValue(value)
        event.accept()


class PlaybackControlBar(QtWidgets.QWidget):
    """Reusable controls for frame navigation and sequence playback."""

    step_requested = QtCore.pyqtSignal(int)
    seek_requested = QtCore.pyqtSignal(int)
    playback_state_requested = QtCore.pyqtSignal(bool)
    # Kept for integrations that only need a generic playback request.
    play_requested = QtCore.pyqtSignal()
    loop_requested = QtCore.pyqtSignal()
    fps_changed = QtCore.pyqtSignal(int)
    loop_radius_changed = QtCore.pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._total_frames = 0
        self._current_frame = 0
        self._is_playing = False
        self._is_looping = False
        self._slider_dragging = False
        self._pause_requested_on_press = False
        self._last_pause_request_ms = None
        self._request_clock = QtCore.QElapsedTimer()
        self._request_clock.start()

        self._create_ui()
        self._connect_signals()
        self._sync_ui()

    @property
    def total_frames(self):
        return self._total_frames

    @property
    def current_frame(self):
        return self._current_frame

    def _create_ui(self):
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(5, 2, 5, 2)
        layout.setSpacing(5)

        self.prev_button = self._media_button(
            QtWidgets.QStyle.StandardPixmap.SP_MediaSkipBackward,
            self.tr("Previous frame"),
        )
        self.play_button = self._media_button(
            QtWidgets.QStyle.StandardPixmap.SP_MediaPlay,
            self.tr("Play"),
        )
        self.play_button.setCheckable(True)
        self.next_button = self._media_button(
            QtWidgets.QStyle.StandardPixmap.SP_MediaSkipForward,
            self.tr("Next frame"),
        )

        self.loop_button = QtWidgets.QPushButton(self.tr("Loop ±N"))
        self.loop_button.setCheckable(True)
        self.loop_button.setIcon(
            self.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_BrowserReload
            )
        )
        self.loop_button.setToolTip(
            self.tr(
                "Loop neighboring frames while annotations stay on the "
                "current anchor frame"
            )
        )

        self.loop_radius_spinbox = QtWidgets.QSpinBox()
        self.loop_radius_spinbox.setRange(0, 999)
        self.loop_radius_spinbox.setValue(3)
        self.loop_radius_spinbox.setPrefix("±")
        self.loop_radius_spinbox.setToolTip(
            self.tr("Number of frames before and after the anchor frame")
        )

        self.progress_slider = AbsoluteSeekSlider(
            QtCore.Qt.Orientation.Horizontal
        )
        self.progress_slider.setRange(0, 0)
        self.progress_slider.setValue(0)
        self.progress_slider.setTracking(True)
        self.progress_slider.setMinimumWidth(160)
        self.progress_slider.setToolTip(self.tr("Playback position"))

        self.frame_label = QtWidgets.QLabel("0 / 0")
        self.frame_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.frame_label.setMinimumWidth(72)

        self.fps_spinbox = QtWidgets.QSpinBox()
        self.fps_spinbox.setRange(1, 120)
        self.fps_spinbox.setValue(30)
        self.fps_spinbox.setSuffix(" FPS")
        self.fps_spinbox.setToolTip(self.tr("Playback frame rate"))

        layout.addWidget(self.prev_button)
        layout.addWidget(self.play_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.loop_button)
        layout.addWidget(self.loop_radius_spinbox)
        layout.addWidget(self.progress_slider, 1)
        layout.addWidget(self.frame_label)
        layout.addWidget(self.fps_spinbox)

    def _media_button(self, standard_pixmap, tooltip):
        button = QtWidgets.QPushButton()
        button.setIcon(self.style().standardIcon(standard_pixmap))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFixedSize(32, 28)
        return button

    def _connect_signals(self):
        self.prev_button.clicked.connect(
            lambda _checked=False: self.step_requested.emit(-1)
        )
        self.next_button.clicked.connect(
            lambda _checked=False: self.step_requested.emit(1)
        )
        self.play_button.pressed.connect(self._on_play_pressed)
        self.play_button.clicked.connect(self._on_play_clicked)
        self.loop_button.clicked.connect(self._on_loop_clicked)
        self.progress_slider.sliderPressed.connect(self._on_slider_pressed)
        self.progress_slider.sliderReleased.connect(self._on_slider_released)
        self.progress_slider.valueChanged.connect(
            self._on_slider_value_changed
        )
        self.fps_spinbox.valueChanged.connect(self.fps_changed.emit)
        self.loop_radius_spinbox.valueChanged.connect(
            self.loop_radius_changed.emit
        )

    def _emit_playback_state_request(self, should_play):
        self.playback_state_requested.emit(bool(should_play))
        self.play_requested.emit()

    def _on_play_pressed(self):
        # Pause on press instead of waiting for release. During high-FPS frame
        # loading, this makes stopping immediate even if the pointer moves off
        # the small button before release.
        self._pause_requested_on_press = False
        if not (self._is_playing or self.play_button.isChecked()):
            return

        self._pause_requested_on_press = True
        self._last_pause_request_ms = self._request_clock.elapsed()
        self._is_playing = False
        self._sync_play_button()
        self._emit_playback_state_request(False)

    def _on_play_clicked(self, checked):
        if self._pause_requested_on_press:
            self._pause_requested_on_press = False
            self._is_playing = False
            self._sync_play_button()
            return

        should_play = bool(checked)
        if should_play and self._last_pause_request_ms is not None:
            elapsed = (
                self._request_clock.elapsed() - self._last_pause_request_ms
            )
            if elapsed < QtWidgets.QApplication.doubleClickInterval():
                # A second click commonly arrives because the first pause did
                # not look immediate. Keep the stopped state instead of
                # interpreting that click as a new play request.
                self._is_playing = False
                self._sync_play_button()
                self._emit_playback_state_request(False)
                return

        if not should_play:
            self._last_pause_request_ms = self._request_clock.elapsed()
        self._is_playing = should_play
        self._sync_play_button()
        self._emit_playback_state_request(should_play)

    def _on_loop_clicked(self, checked):
        self._is_looping = bool(checked)
        self.loop_requested.emit()

    def _on_slider_pressed(self):
        self._slider_dragging = True

    def _on_slider_released(self):
        self._slider_dragging = False
        if self._total_frames <= 0:
            return
        self._current_frame = self._clamp_frame(self.progress_slider.value())
        self._sync_frame_ui()
        self.seek_requested.emit(self._current_frame)

    def _on_slider_value_changed(self, frame_index):
        if self._total_frames <= 0:
            return
        self._current_frame = self._clamp_frame(frame_index)
        self._sync_frame_ui()
        if not self._slider_dragging:
            self.seek_requested.emit(self._current_frame)

    def _clamp_frame(self, frame_index):
        if self._total_frames <= 0:
            return 0
        return min(max(0, int(frame_index)), self._total_frames - 1)

    @staticmethod
    def _set_value_without_signals(widget, value):
        signals_were_blocked = widget.blockSignals(True)
        try:
            widget.setValue(value)
        finally:
            widget.blockSignals(signals_were_blocked)

    def _sync_play_button(self):
        if self._is_playing:
            icon = QtWidgets.QStyle.StandardPixmap.SP_MediaPause
            tooltip = self.tr("Pause")
        else:
            icon = QtWidgets.QStyle.StandardPixmap.SP_MediaPlay
            tooltip = self.tr("Play")
        self.play_button.setIcon(self.style().standardIcon(icon))
        self.play_button.setToolTip(tooltip)
        self.play_button.setAccessibleName(tooltip)
        self.play_button.setChecked(self._is_playing)

    def _sync_frame_ui(self):
        if self._total_frames <= 0:
            self.frame_label.setText("0 / 0")
        else:
            self.frame_label.setText(
                f"{self._current_frame + 1} / {self._total_frames}"
            )

        has_previous = self._total_frames > 0 and self._current_frame > 0
        has_next = (
            self._total_frames > 0
            and self._current_frame < self._total_frames - 1
        )
        self.prev_button.setEnabled(has_previous)
        self.next_button.setEnabled(has_next)

    def _sync_ui(self):
        has_multiple_frames = self._total_frames > 1
        if not has_multiple_frames:
            self._is_playing = False
            self._is_looping = False

        self.play_button.setEnabled(has_multiple_frames)
        self.loop_button.setEnabled(has_multiple_frames)
        self.progress_slider.setEnabled(has_multiple_frames)
        self.loop_button.setChecked(self._is_looping)
        self._sync_play_button()
        self._sync_frame_ui()

    def set_total_frames(self, total):
        """Set the sequence length and clamp the current frame."""

        total = max(0, int(total))
        if total == self._total_frames:
            self._sync_ui()
            return

        self._total_frames = total
        self._current_frame = self._clamp_frame(self._current_frame)

        signals_were_blocked = self.progress_slider.blockSignals(True)
        try:
            self.progress_slider.setRange(0, max(0, self._total_frames - 1))
            self.progress_slider.setValue(self._current_frame)
        finally:
            self.progress_slider.blockSignals(signals_were_blocked)
        self._sync_ui()

    def set_current_frame(self, frame_index):
        """Set the current zero-based frame without requesting a seek."""

        if self._slider_dragging:
            return
        self._current_frame = self._clamp_frame(frame_index)
        self._set_value_without_signals(
            self.progress_slider, self._current_frame
        )
        self._sync_frame_ui()

    def set_playback_state(self, is_playing):
        """Update the play/pause presentation without emitting a request."""

        self._is_playing = bool(is_playing) and self._total_frames > 1
        self._sync_play_button()

    def set_loop_state(self, is_looping):
        """Update the loop presentation without emitting a request."""

        self._is_looping = bool(is_looping) and self._total_frames > 1
        self.loop_button.setChecked(self._is_looping)

    def set_fps(self, fps):
        """Set and clamp the displayed FPS without emitting ``fps_changed``."""

        fps = min(max(1, int(fps)), 120)
        self._set_value_without_signals(self.fps_spinbox, fps)


# Backward-compatible short name for integrations that imported the first
# version of this standalone widget.
PlaybackControl = PlaybackControlBar
