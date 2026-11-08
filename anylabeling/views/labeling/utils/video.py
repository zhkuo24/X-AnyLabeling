import hashlib
import json
import os
import os.path as osp
import shutil
import subprocess
import tempfile

import cv2

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QMessageBox,
    QProgressDialog,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QApplication,
)

from anylabeling.views.labeling.chatbot.style import ChatbotDialogStyle
from anylabeling.views.labeling.logger import logger
from anylabeling.views.labeling.utils.media import (
    PRESERVED_SIDECAR_EXTENSIONS,
    install_extracted_directory,
)
from anylabeling.views.labeling.utils.qt import new_icon_path
from anylabeling.views.labeling.utils.style import (
    get_msg_box_style,
    get_progress_dialog_style,
    get_ok_btn_style,
    get_cancel_btn_style,
)
from anylabeling.views.labeling.widgets import Popup


class FrameExtractionDialog(QDialog):
    def __init__(self, parent=None, total_frames=0, fps=0):
        super().__init__(parent)
        self.total_frames = total_frames
        self.fps = fps
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle(self.tr("Frame Extraction Settings"))
        self.setStyleSheet(get_msg_box_style())
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(12, 8, 12, 12)

        # Interval input
        interval_layout = QHBoxLayout()
        template = self.tr("Frame interval (fps: %.2f):")
        interval_label = QLabel(template % self.fps)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, max(1, self.total_frames))
        self.interval_spin.setValue(1)
        self.interval_spin.setStyleSheet(
            ChatbotDialogStyle.get_spinbox_style(
                up_arrow_url=new_icon_path("caret-up", "svg"),
                down_arrow_url=new_icon_path("caret-down", "svg"),
            )
        )
        self.interval_spin.setMinimumWidth(100)
        interval_layout.addWidget(interval_label)
        interval_layout.addWidget(self.interval_spin)

        # Prefix input
        prefix_layout = QHBoxLayout()
        prefix_label = QLabel(self.tr("Filename prefix:"))
        self.prefix_edit = QLineEdit()
        base_style = ChatbotDialogStyle.get_settings_edit_style()
        self.prefix_edit.setStyleSheet(base_style + """
            QLineEdit {
                padding-top: 6px;
                padding-right: 8px;
                padding-bottom: 0px;
                padding-left: 8px;
                min-height: 28px;
            }
            """)
        self.prefix_edit.setText("frame_")
        prefix_layout.addWidget(prefix_label)
        prefix_layout.addWidget(self.prefix_edit)
        prefix_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Sequence length input
        seq_layout = QHBoxLayout()
        seq_label = QLabel(self.tr("Number sequence length:"))
        self.seq_spin = QSpinBox()
        self.seq_spin.setRange(3, 10)
        self.seq_spin.setValue(5)
        self.seq_spin.setMinimumWidth(100)
        self.seq_spin.setStyleSheet(
            ChatbotDialogStyle.get_spinbox_style(
                up_arrow_url=new_icon_path("caret-up", "svg"),
                down_arrow_url=new_icon_path("caret-down", "svg"),
            )
        )
        seq_layout.addWidget(seq_label)
        seq_layout.addWidget(self.seq_spin)

        layout.addLayout(prefix_layout)
        layout.addLayout(seq_layout)
        layout.addLayout(interval_layout)

        # Example output
        self.example_label = QLabel()
        self.update_example()
        layout.addWidget(self.example_label)

        # Buttons
        button_layout = QHBoxLayout()
        ok_button = QPushButton(self.tr("OK"))
        ok_button.setStyleSheet(get_ok_btn_style())
        cancel_button = QPushButton(self.tr("Cancel"))
        cancel_button.setStyleSheet(get_cancel_btn_style())

        ok_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)

        button_layout.addWidget(ok_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)

        # Connect signals
        self.prefix_edit.textChanged.connect(self.update_example)
        self.seq_spin.valueChanged.connect(self.update_example)

        self.setLayout(layout)
        self.resize(480, 240)

    def update_example(self):
        example = f"{self.prefix_edit.text()}{str(1).zfill(self.seq_spin.value())}.jpg"
        template = self.tr("Example output: {example}")
        self.example_label.setText(template.format(example=example))

    def get_values(self):
        return (
            self.interval_spin.value(),
            self.prefix_edit.text(),
            self.seq_spin.value(),
        )


# Written beside extracted frames. It is a sidecar, so a later install can
# carry it forward, but it is not an annotation.
FRAME_SOURCE_MANIFEST = ".xany-frame-source.json"
_MANIFEST_VERSION = 1
_OPENCV_SELECTION = "frame_index_mod_interval"
_FFMPEG_SELECTION = "ffmpeg_fps_filter"
_MANIFEST_IDENTITY_KEYS = (
    "version",
    "source_sha256",
    "source_size",
    "extractor",
    "interval",
    "prefix",
    "seq_len",
    "frame_selection",
    "naming",
)


class ExtractionCancelled(Exception):
    """The user cancelled frame extraction before it finished."""


class VideoFrameIdentityError(Exception):
    """Installing these frames would reuse annotations for other source frames."""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frame_filename(prefix, index, seq_len):
    return f"{prefix}{str(index).zfill(int(seq_len))}.jpg"


def _selection_for(extractor):
    if extractor == "ffmpeg":
        return _FFMPEG_SELECTION
    if extractor == "opencv":
        return _OPENCV_SELECTION
    raise ValueError(f"Unknown frame extractor: {extractor}")


def write_frame_source_manifest(
    out_dir,
    *,
    source_path,
    extractor,
    interval,
    prefix,
    seq_len,
    fps,
):
    """Record the source file and sampling rule that produced ``out_dir``."""

    source_stat = os.stat(source_path)
    manifest = {
        "version": _MANIFEST_VERSION,
        "source_sha256": _sha256(source_path),
        "source_size": int(source_stat.st_size),
        "extractor": extractor,
        "interval": int(interval),
        "prefix": prefix,
        "seq_len": int(seq_len),
        "fps": f"{float(fps):.6f}",
        "frame_selection": _selection_for(extractor),
        "naming": "output_index",
    }
    target = osp.join(out_dir, FRAME_SOURCE_MANIFEST)
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(
            manifest,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")
    os.replace(temporary, target)
    return manifest


def read_frame_source_manifest(directory):
    """Return the sampling manifest, or None when it is missing or unreadable."""

    path = osp.join(directory, FRAME_SOURCE_MANIFEST)
    if not osp.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    return manifest


def sampling_manifests_match(existing, extracted):
    """True only when both manifests prove the same source frames."""

    if not isinstance(existing, dict) or not isinstance(extracted, dict):
        return False
    for key in _MANIFEST_IDENTITY_KEYS:
        if existing.get(key) != extracted.get(key):
            return False
    # OpenCV steps by decoded frame index. ffmpeg's fps filter also depends
    # on the fps value that was baked into the command.
    if existing.get("extractor") == "ffmpeg":
        return existing.get("fps") == extracted.get("fps")
    return True


def _directory_has_user_sidecars(directory):
    if not osp.isdir(directory):
        return False
    for root, _dirs, filenames in os.walk(directory):
        for filename in filenames:
            if filename == FRAME_SOURCE_MANIFEST:
                continue
            suffix = osp.splitext(filename)[1].lower()
            if suffix in PRESERVED_SIDECAR_EXTENSIONS:
                return True
    return False


def ensure_video_frame_identity(existing_dir, extracted_dir):
    """Refuse to replace a directory whose annotations are not proven to match.

    A directory with no annotation sidecars can still be replaced. DICOM
    installs do not call this; their filenames already are source indexes.
    """

    if not osp.isdir(existing_dir) or osp.islink(existing_dir):
        return
    if not _directory_has_user_sidecars(existing_dir):
        return
    existing = read_frame_source_manifest(existing_dir)
    extracted = read_frame_source_manifest(extracted_dir)
    if sampling_manifests_match(existing, extracted):
        return
    raise VideoFrameIdentityError(
        "The existing directory has annotations, but this extraction is "
        "not proven to be the same source frames."
    )


def install_video_extraction(temporary_dir, output_dir):
    """Install video frames, or leave ``output_dir`` untouched."""

    ensure_video_frame_identity(output_dir, temporary_dir)
    # The manifest ends in .json, so a generic sidecar copy would replace the
    # new sampling record with the previous one.
    install_extracted_directory(
        temporary_dir,
        output_dir,
        skip_sidecar_names=(FRAME_SOURCE_MANIFEST,),
    )


def extract_sampled_frames(
    source_path,
    out_dir,
    *,
    extractor,
    interval,
    prefix,
    seq_len,
    fps,
    video_path=None,
    ffmpeg_path=None,
    is_cancelled=None,
    on_tick=None,
    on_saved=None,
):
    """Extract frames with one sampler and record how they were chosen.

    ``source_path`` is the file the user selected. ``video_path`` is the
    readable copy when opening the original needed a temporary file.
    """

    interval = int(interval)
    seq_len = int(seq_len)
    if interval < 1:
        raise ValueError("Frame interval must be at least 1.")
    decode_path = video_path or source_path
    os.makedirs(out_dir, exist_ok=True)

    if extractor == "ffmpeg":
        saved = _extract_with_ffmpeg(
            decode_path,
            out_dir,
            ffmpeg_path=ffmpeg_path,
            interval=interval,
            prefix=prefix,
            seq_len=seq_len,
            fps=fps,
            is_cancelled=is_cancelled,
            on_tick=on_tick,
        )
    elif extractor == "opencv":
        saved = _extract_with_opencv(
            decode_path,
            out_dir,
            interval=interval,
            prefix=prefix,
            seq_len=seq_len,
            is_cancelled=is_cancelled,
            on_tick=on_tick,
            on_saved=on_saved,
        )
    else:
        raise ValueError(f"Unknown frame extractor: {extractor}")

    if saved <= 0:
        raise RuntimeError("Video extraction produced no readable frames.")
    write_frame_source_manifest(
        out_dir,
        source_path=source_path,
        extractor=extractor,
        interval=interval,
        prefix=prefix,
        seq_len=seq_len,
        fps=fps,
    )
    return saved


def _extract_with_ffmpeg(
    video_path,
    out_dir,
    *,
    ffmpeg_path,
    interval,
    prefix,
    seq_len,
    fps,
    is_cancelled,
    on_tick,
):
    if not ffmpeg_path:
        raise FileNotFoundError("ffmpeg not found.")
    output_fps = fps / interval if interval > 0 else fps
    output_pattern = osp.join(out_dir, f"{prefix}%0{seq_len}d.jpg")
    command = [
        ffmpeg_path,
        "-i",
        video_path,
        "-vf",
        f"fps={output_fps}",
        "-qscale:v",
        "2",
        "-start_number",
        "0",
        output_pattern,
    ]
    logger.info(f"Running ffmpeg command: {' '.join(command)}")
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        while process.poll() is None:
            if on_tick is not None:
                on_tick()
            if is_cancelled is not None and is_cancelled():
                _terminate_process(process)
                raise ExtractionCancelled()
        if process.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed with exit code {process.returncode}"
            )
    except ExtractionCancelled:
        raise
    except Exception:
        if process.poll() is None:
            _terminate_process(process)
        raise
    return _count_extracted_jpgs(out_dir, prefix)


def _extract_with_opencv(
    video_path,
    out_dir,
    *,
    interval,
    prefix,
    seq_len,
    is_cancelled,
    on_tick,
    on_saved,
):
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"Failed to open video file: {video_path}")
    frame_count = 0
    saved = 0
    try:
        while True:
            if on_tick is not None:
                on_tick()
            if is_cancelled is not None and is_cancelled():
                raise ExtractionCancelled()
            read_ok, frame = capture.read()
            if not read_ok:
                break
            if frame_count % interval == 0:
                frame_path = osp.join(
                    out_dir, _frame_filename(prefix, saved, seq_len)
                )
                if not cv2.imwrite(frame_path, frame):
                    raise OSError(f"Failed to write frame: {frame_path}")
                saved += 1
                if on_saved is not None:
                    on_saved(saved)
            frame_count += 1
    finally:
        capture.release()
    return saved


def _count_extracted_jpgs(out_dir, prefix):
    return len(
        [
            name
            for name in os.listdir(out_dir)
            if name.startswith(prefix) and name.lower().endswith(".jpg")
        ]
    )


def _terminate_process(process):
    try:
        process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _run_sampled_extraction(
    self,
    *,
    source_path,
    decode_path,
    out_dir,
    video_capture,
    total_frames,
    fps,
    interval,
    prefix,
    seq_len,
):
    """Run ffmpeg or OpenCV into ``out_dir`` and record the sampling rule."""

    ffmpeg_path = shutil.which("ffmpeg")
    if video_capture is not None and video_capture.isOpened():
        video_capture.release()
    extractor = "ffmpeg" if ffmpeg_path else "opencv"
    if extractor == "ffmpeg":
        logger.info(f"Detected ffmpeg for extraction: {ffmpeg_path}")
        progress_label = self.tr("Extracting frames using ffmpeg...")
        progress_maximum = 0
    else:
        logger.info("ffmpeg not found. Using OpenCV for extraction.")
        progress_label = self.tr(
            "Extracting frames (OpenCV)... Please wait..."
        )
        progress_maximum = (
            (total_frames + interval - 1) // interval
            if total_frames > 0 and interval > 0
            else 0
        )

    progress_dialog = QProgressDialog(
        progress_label,
        self.tr("Cancel"),
        0,
        progress_maximum,
        self,
    )
    progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
    progress_dialog.setWindowTitle(self.tr("Progress"))
    progress_dialog.setMinimumWidth(400)
    progress_dialog.setMinimumHeight(150)
    progress_dialog.setStyleSheet(
        get_progress_dialog_style(color="#1d1d1f", height=20)
    )
    progress_dialog.setValue(0)
    progress_dialog.show()
    QApplication.processEvents()

    def report_saved(count, dialog=progress_dialog):
        dialog.setMaximum(max(dialog.maximum(), count))
        dialog.setValue(count)

    try:
        extract_sampled_frames(
            source_path,
            out_dir,
            extractor=extractor,
            interval=interval,
            prefix=prefix,
            seq_len=seq_len,
            fps=fps,
            video_path=decode_path,
            ffmpeg_path=ffmpeg_path,
            is_cancelled=progress_dialog.wasCanceled,
            on_tick=QApplication.processEvents,
            on_saved=report_saved if extractor == "opencv" else None,
        )
    except ExtractionCancelled:
        logger.info("Frame extraction cancelled.")
        return None
    finally:
        if progress_dialog.isVisible():
            progress_dialog.close()

    effective_fps = fps / interval if interval > 0 else fps
    return out_dir, effective_fps


def extract_frames_from_video(self, input_file, out_dir):
    """Extract frames and return ``(output_dir, effective_fps)`` on success."""

    temp_video_path = None
    video_capture = None
    opened_successfully = False

    try:
        input_file_str = str(input_file)

        # Load video directly
        video_capture = cv2.VideoCapture(input_file_str)
        if video_capture.isOpened():
            opened_successfully = True
        else:
            video_capture.release()
            logger.warning(
                f"Loading video failed. Trying temporary file workaround."
            )

            try:
                _, ext = osp.splitext(input_file)
                suffix = ext if ext else ".mp4"
                temp_file = tempfile.NamedTemporaryFile(
                    suffix=suffix, delete=False
                )
                temp_video_path = temp_file.name
                temp_file.close()
                shutil.copy2(input_file, temp_video_path)
                logger.debug(
                    f"Writing video data to temporary file: {temp_video_path}"
                )

                video_capture = cv2.VideoCapture(temp_video_path)
                if video_capture.isOpened():
                    opened_successfully = True
                else:
                    video_capture.release()
                    logger.error(
                        f"Failed to open video via temporary file: {temp_video_path}"
                    )
            except Exception as e:
                logger.error(f"Error during temporary file workaround: {e}")
                if video_capture:
                    video_capture.release()

        if not opened_successfully:
            popup = Popup(
                f"Failed to open video file: {osp.basename(input_file)}",
                self,
                icon=new_icon_path("warning", "svg"),
            )
            popup.show_popup(self, position="center")
            return None

        # --- Proceed with frame extraction settings ---
        total_frames = int(video_capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = video_capture.get(cv2.CAP_PROP_FPS)
        # Handle cases where fps might be 0 or invalid
        if not fps or fps <= 0:
            logger.warning(
                f"Invalid or zero FPS ({fps}) detected for video. Defaulting FPS to 30 for calculations."
            )
            fps = 30.0  # Assign a default FPS
        logger.info(
            f"Video opened: Total Frames ~{total_frames}, FPS ~{fps:.2f}"
        )

        dialog = FrameExtractionDialog(self, total_frames, fps)
        if not dialog.exec():
            logger.info(
                "Frame extraction cancelled by user in settings dialog."
            )
            # video_capture is released in the outer finally block
            return None

        interval, prefix, seq_len = dialog.get_values()
        if (
            osp.isabs(prefix)
            or prefix in {".", ".."}
            or "/" in prefix
            or "\\" in prefix
            or "%" in prefix
            or ":" in prefix
        ):
            popup = Popup(
                self.tr(
                    "Filename prefix must be a name, not a path, and cannot "
                    "contain '/', '\\\\', ':', or '%'."
                ),
                self,
                icon=new_icon_path("warning", "svg"),
            )
            popup.show_popup(self, position="center")
            return None
        os.makedirs(out_dir, exist_ok=True)

        # Inner try: Handle the actual extraction (ffmpeg or OpenCV)
        try:
            return _run_sampled_extraction(
                self,
                source_path=input_file_str,
                decode_path=(
                    temp_video_path if temp_video_path else input_file_str
                ),
                out_dir=out_dir,
                video_capture=video_capture,
                total_frames=total_frames,
                fps=fps,
                interval=interval,
                prefix=prefix,
                seq_len=seq_len,
            )

        # Except block for the *inner* try (extraction phase: ffmpeg or OpenCV)
        except Exception as extraction_e:
            logger.exception(
                f"An unexpected error occurred during frame extraction logic: {extraction_e}"
            )
            popup = Popup(
                f"An unexpected error occurred during extraction: {extraction_e}",
                self,
                icon=new_icon_path("warning", "svg"),
            )
            popup.show_popup(self, position="center")
            return None  # Indicate failure of extraction phase

    # Except block for the *outer* try (opening/setup phase)
    except Exception as opening_e:
        logger.exception(
            f"An unexpected error occurred during video opening/setup: {opening_e}"
        )
        # Use Popup instead of QMessageBox
        popup = Popup(
            f"An error occurred during setup: {opening_e}",
            self,
            icon=new_icon_path("error", "svg"),
        )
        popup.show_popup(self, position="center")
        return None  # Indicate failure

    # Finally block for the *outer* try (always runs)
    finally:
        # Release capture if it exists and is opened (mainly for OpenCV path or if ffmpeg failed early)
        if video_capture is not None and video_capture.isOpened():
            logger.info("Releasing video capture resource.")
            video_capture.release()
        # Clean up the temporary file if created
        if temp_video_path and osp.exists(temp_video_path):
            try:
                logger.debug(
                    f"Removing temporary video file: {temp_video_path}"
                )
                os.remove(temp_video_path)
            except OSError as e:
                logger.error(
                    f"Error removing temporary file {temp_video_path}: {e}"
                )


def open_video_file(self):
    if not self.may_continue():
        return

    file_filter = "Video Files (*.asf *.avi *.m4v *.mkv *.mov *.mp4 *.mpeg *.mpg *.ts *.wmv);;All Files (*)"
    input_file, _ = QFileDialog.getOpenFileName(
        self,
        self.tr("Open Video file"),
        "",
        file_filter,
    )

    if not input_file or not osp.exists(input_file):
        logger.warning(
            f"No valid video file selected or file does not exist: {input_file}"
        )
        return

    out_dir = osp.join(
        osp.dirname(input_file), osp.splitext(osp.basename(input_file))[0]
    )

    if osp.lexists(out_dir) and (
        not osp.isdir(out_dir) or osp.islink(out_dir)
    ):
        popup = Popup(
            f"Frame output path exists but is not a directory: {out_dir}",
            self,
            icon=new_icon_path("error", "svg"),
        )
        popup.show_popup(self, position="center")
        return

    if osp.lexists(out_dir):
        response = QMessageBox()
        response.setIcon(QMessageBox.Icon.Warning)
        response.setWindowTitle(self.tr("Warning"))
        response.setText(self.tr("Directory Already Exists"))

        template = (
            "Directory '{}' already exists. Do you want to overwrite it?"
        )
        translated_template = self.tr(template)
        final_text = translated_template.format(osp.basename(out_dir))
        response.setInformativeText(final_text)
        response.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok
        )
        response.setDefaultButton(QMessageBox.StandardButton.Ok)
        response.setStyleSheet(get_msg_box_style())

        if response.exec() != QMessageBox.StandardButton.Ok:
            logger.info(
                f"User chose not to overwrite existing directory: {out_dir}"
            )
            return

    temporary_dir = None
    try:
        output_parent = osp.dirname(out_dir) or "."
        temporary_dir = tempfile.mkdtemp(
            prefix=f".{osp.basename(out_dir)}-",
            dir=output_parent,
        )
        logger.info(
            f"Starting frame extraction for: {input_file} -> {temporary_dir}"
        )
        extraction_result = extract_frames_from_video(
            self, input_file, temporary_dir
        )
        if not extraction_result:
            logger.warning(
                f"Frame extraction failed or was cancelled for: {input_file}"
            )
            return

        result_dir, effective_fps = extraction_result
        if osp.abspath(result_dir) != osp.abspath(temporary_dir):
            raise RuntimeError(
                "Video extraction returned an unexpected output directory."
            )
        try:
            install_video_extraction(temporary_dir, out_dir)
        except VideoFrameIdentityError:
            # Same output names can be different source frames after the
            # interval or decoder changes. Leave the old directory in place.
            logger.warning(
                "Refusing to replace %s because frame identity is unproven",
                out_dir,
            )
            QMessageBox.warning(
                self,
                self.tr("Annotations do not match these frames"),
                self.tr(
                    "The existing directory has annotations, but this "
                    "extraction is not proven to be the same source frames. "
                    "The original directory was left unchanged. Move or "
                    "rename it before extracting with a different interval "
                    "or decoder."
                ),
            )
            return
        temporary_dir = None
        playback_fps_setter = getattr(self, "set_playback_fps", None)
        if callable(playback_fps_setter):
            playback_fps_setter(effective_fps)
        logger.info(
            f"Frame extraction process finished for directory: {out_dir}"
        )
        self.import_image_folder(out_dir)
    except Exception as error:
        logger.exception(f"Failed to install extracted video frames: {error}")
        popup = Popup(
            f"Failed to install extracted video frames: {error}",
            self,
            icon=new_icon_path("error", "svg"),
        )
        popup.show_popup(self, position="center")
    finally:
        if temporary_dir is not None and osp.exists(temporary_dir):
            shutil.rmtree(temporary_dir, ignore_errors=True)
