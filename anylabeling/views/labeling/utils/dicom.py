"""DICOM image-run loading and frame extraction utilities.

The labeling canvas works with ordinary image files.  This module bridges
single-frame and multi-frame DICOM objects to that workflow by rendering their
display pixels to a lossless PNG sequence.

``.run`` is not a standard DICOM extension.  Some medical systems use "run"
to describe an acquisition and may store a regular DICOM object with that
suffix.  Such files are accepted by content.  Proprietary RUN containers are
rejected with an explicit error instead of being interpreted as arbitrary
binary data.
"""

from __future__ import annotations

import os
import os.path as osp
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pydicom
from PIL import Image
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
    QProgressDialog,
)

from anylabeling.views.labeling.logger import logger
from anylabeling.views.labeling.utils.media import (
    install_extracted_directory,
)
from anylabeling.views.labeling.utils.style import (
    get_msg_box_style,
    get_progress_dialog_style,
)

try:
    from pydicom.pixels import (
        apply_color_lut,
        apply_modality_lut,
        apply_voi_lut,
    )
except ImportError:  # pragma: no cover - compatibility with pydicom 2.x
    from pydicom.pixel_data_handlers.util import (
        apply_color_lut,
        apply_modality_lut,
        apply_voi_lut,
    )


DICOM_EXTENSIONS = (".dcm", ".dicom", ".ima", ".run")
DEFAULT_DICOM_FPS = 30.0


class DicomError(RuntimeError):
    """Base error raised for unsupported or invalid DICOM image data."""


class DicomReadError(DicomError):
    """Raised when a file cannot be recognized as a DICOM image object."""


class DicomDecodeError(DicomError):
    """Raised when a DICOM object is valid but its pixels cannot be decoded."""


@dataclass(frozen=True)
class DicomExtractionResult:
    """Result returned after materializing a DICOM object as PNG frames."""

    output_dir: str
    frame_paths: tuple[str, ...]
    fps: float
    cancelled: bool = False


def _suffix(path: os.PathLike | str) -> str:
    return Path(os.fspath(path)).suffix.lower()


def _private_run_message(path: os.PathLike | str) -> str:
    return (
        f"'{osp.basename(os.fspath(path))}' is not a readable DICOM image "
        "run. Proprietary RUN containers require a vendor-specific decoder "
        "and are not currently supported."
    )


def _validate_image_dataset(dataset: Dataset) -> None:
    pixel_keywords = ("PixelData", "FloatPixelData", "DoubleFloatPixelData")
    if not any(keyword in dataset for keyword in pixel_keywords):
        raise DicomReadError("The DICOM object does not contain pixel data.")

    try:
        rows = int(dataset.Rows)
        columns = int(dataset.Columns)
    except (AttributeError, TypeError, ValueError) as error:
        raise DicomReadError(
            "The DICOM object has no valid Rows and Columns metadata."
        ) from error

    if rows <= 0 or columns <= 0:
        raise DicomReadError("The DICOM object has invalid image dimensions.")


def load_dicom_dataset(path: os.PathLike | str) -> Dataset:
    """Read and validate a DICOM image object.

    Strict Part 10 parsing is attempted first.  A forced parse is then used for
    valid DICOM datasets without a preamble, which are common in exported
    medical data.  Forced parsing is accepted only when image metadata and
    pixel data are present, preventing arbitrary ``.run`` files from being
    treated as DICOM.
    """

    file_path = os.fspath(path)
    if not osp.isfile(file_path):
        raise DicomReadError(f"DICOM file does not exist: {file_path}")

    strict_error = None
    try:
        dataset = pydicom.dcmread(file_path, force=False)
    except (InvalidDicomError, OSError, ValueError) as error:
        strict_error = error
        try:
            dataset = pydicom.dcmread(file_path, force=True)
        except Exception as forced_error:
            if _suffix(file_path) == ".run":
                raise DicomReadError(
                    _private_run_message(file_path)
                ) from forced_error
            raise DicomReadError(
                f"Unable to read DICOM file '{osp.basename(file_path)}': "
                f"{forced_error}"
            ) from forced_error

    try:
        _validate_image_dataset(dataset)
    except DicomReadError as validation_error:
        if _suffix(file_path) == ".run":
            raise DicomReadError(
                _private_run_message(file_path)
            ) from validation_error
        if strict_error is not None:
            raise DicomReadError(
                f"'{osp.basename(file_path)}' is not a readable DICOM image."
            ) from strict_error
        raise

    return dataset


def is_dicom_file(path: os.PathLike | str) -> bool:
    """Return whether ``path`` contains a readable DICOM image object."""

    try:
        load_dicom_dataset(path)
    except DicomError:
        return False
    return True


def _value_items(value) -> Iterable:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value,)
    try:
        return iter(value)
    except TypeError:
        return (value,)


def _first_number(value) -> float | None:
    for item in _value_items(value):
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if np.isfinite(number):
            return number
    return None


def get_dicom_fps(
    dataset: Dataset, default: float = DEFAULT_DICOM_FPS
) -> float:
    """Resolve display FPS from standard DICOM timing attributes."""

    for keyword in ("RecommendedDisplayFrameRate", "CineRate"):
        value = _first_number(getattr(dataset, keyword, None))
        if value is not None and value > 0:
            return value

    frame_time = _first_number(getattr(dataset, "FrameTime", None))
    if frame_time is not None and frame_time > 0:
        return 1000.0 / frame_time

    frame_time_vector = getattr(dataset, "FrameTimeVector", None)
    if frame_time_vector is not None:
        timings = []
        for item in _value_items(frame_time_vector):
            try:
                timing = float(item)
            except (TypeError, ValueError):
                continue
            if np.isfinite(timing) and timing > 0:
                timings.append(timing)
        if timings:
            return 1000.0 / float(np.mean(timings))

    try:
        fallback = float(default)
    except (TypeError, ValueError):
        fallback = DEFAULT_DICOM_FPS
    return (
        fallback
        if np.isfinite(fallback) and fallback > 0
        else DEFAULT_DICOM_FPS
    )


def get_dicom_frame_count(dataset: Dataset) -> int:
    """Return the declared frame count, defaulting to one."""

    try:
        frame_count = int(getattr(dataset, "NumberOfFrames", 1))
    except (TypeError, ValueError):
        frame_count = 1
    return max(1, frame_count)


def split_dicom_frames(
    pixel_array: np.ndarray, frame_count: int
) -> tuple[np.ndarray, ...]:
    """Split decoded pixels using explicit DICOM frame metadata.

    Array dimensionality alone is ambiguous: ``(H, W, 3)`` can be one RGB
    frame or ``H`` grayscale frames.  ``NumberOfFrames`` is therefore the
    authority.
    """

    pixels = np.asarray(pixel_array)
    declared_count = max(1, int(frame_count))

    if declared_count == 1:
        return (pixels,)

    if pixels.ndim < 3 or pixels.shape[0] != declared_count:
        raise DicomDecodeError(
            "Decoded pixel data does not match NumberOfFrames "
            f"({pixels.shape!r} versus {declared_count})."
        )
    return tuple(pixels[index] for index in range(declared_count))


def _normalize_to_uint8(values: np.ndarray) -> np.ndarray:
    array = np.asarray(values)
    if array.dtype == np.uint8:
        return array.copy()
    if array.dtype == np.bool_:
        return array.astype(np.uint8) * 255

    working = array.astype(np.float64, copy=False)
    finite = np.isfinite(working)
    if not finite.any():
        return np.zeros(array.shape, dtype=np.uint8)

    minimum = float(np.min(working[finite]))
    maximum = float(np.max(working[finite]))
    if maximum <= minimum:
        constant = int(round(np.clip(minimum, 0.0, 255.0)))
        result = np.full(array.shape, constant, dtype=np.uint8)
        result[~finite] = 0
        return result

    scaled = (working - minimum) * 255.0 / (maximum - minimum)
    scaled[~finite] = 0.0
    return np.clip(np.rint(scaled), 0, 255).astype(np.uint8)


def _window_to_uint8(
    values: np.ndarray, center: float, width: float
) -> np.ndarray:
    if not np.isfinite(center) or not np.isfinite(width) or width <= 0:
        return _normalize_to_uint8(values)

    working = np.asarray(values, dtype=np.float64)
    if width <= 1:
        return np.where(working > center - 0.5, 255, 0).astype(np.uint8)

    lower = center - 0.5 - (width - 1.0) / 2.0
    upper = center - 0.5 + (width - 1.0) / 2.0
    result = np.empty(working.shape, dtype=np.float64)
    result[working <= lower] = 0.0
    result[working > upper] = 255.0
    middle = (working > lower) & (working <= upper)
    result[middle] = (
        (working[middle] - (center - 0.5)) / (width - 1.0) + 0.5
    ) * 255.0
    result[~np.isfinite(working)] = 0.0
    return np.clip(np.rint(result), 0, 255).astype(np.uint8)


def _is_color_frame(frame: np.ndarray, dataset: Dataset) -> bool:
    try:
        samples_per_pixel = int(getattr(dataset, "SamplesPerPixel", 1))
    except (TypeError, ValueError):
        samples_per_pixel = 1
    photometric = str(
        getattr(dataset, "PhotometricInterpretation", "")
    ).upper()
    return (
        samples_per_pixel > 1
        or photometric in {"RGB", "PALETTE COLOR"}
        or photometric.startswith("YBR")
        or (frame.ndim == 3 and frame.shape[-1] in (3, 4))
    )


def prepare_dicom_frame(frame: np.ndarray, dataset: Dataset) -> np.ndarray:
    """Convert one decoded DICOM frame to display-ready uint8 pixels.

    Grayscale pixels use modality rescaling, window center/width or a VOI LUT
    when available, and otherwise min/max normalization.  MONOCHROME1 is
    inverted after display conversion.  Color frames are returned in RGB(A)
    order, as expected by Pillow when writing PNG files.
    """

    pixels = np.asarray(frame)
    if _is_color_frame(pixels, dataset):
        photometric = str(
            getattr(dataset, "PhotometricInterpretation", "")
        ).upper()
        if photometric == "PALETTE COLOR":
            try:
                pixels = apply_color_lut(pixels, dataset)
            except Exception as error:
                raise DicomDecodeError(
                    "Unable to apply the DICOM palette color LUT."
                ) from error

        if (
            pixels.ndim == 3
            and pixels.shape[-1] not in (3, 4)
            and pixels.shape[0] in (3, 4)
        ):
            pixels = np.moveaxis(pixels, 0, -1)
        if pixels.ndim != 3 or pixels.shape[-1] not in (3, 4):
            raise DicomDecodeError(
                f"Unsupported DICOM color pixel shape: {pixels.shape!r}"
            )
        return _normalize_to_uint8(pixels)

    if pixels.ndim == 3 and pixels.shape[-1] == 1:
        pixels = pixels[..., 0]
    elif pixels.ndim == 3 and pixels.shape[0] == 1:
        pixels = pixels[0]
    if pixels.ndim != 2:
        raise DicomDecodeError(
            f"Unsupported DICOM grayscale pixel shape: {pixels.shape!r}"
        )

    try:
        display_values = apply_modality_lut(pixels, dataset)
    except Exception as error:
        raise DicomDecodeError(
            "Unable to apply DICOM modality rescaling."
        ) from error

    center = _first_number(getattr(dataset, "WindowCenter", None))
    width = _first_number(getattr(dataset, "WindowWidth", None))
    if center is not None and width is not None:
        result = _window_to_uint8(display_values, center, width)
    elif getattr(dataset, "VOILUTSequence", None):
        try:
            result = _normalize_to_uint8(
                apply_voi_lut(display_values, dataset)
            )
        except Exception as error:
            raise DicomDecodeError(
                "Unable to apply the DICOM VOI LUT."
            ) from error
    else:
        result = _normalize_to_uint8(display_values)

    if (
        str(getattr(dataset, "PhotometricInterpretation", "")).upper()
        == "MONOCHROME1"
    ):
        result = 255 - result
    return result


def _decode_pixel_array(dataset: Dataset) -> np.ndarray:
    try:
        return dataset.pixel_array
    except Exception as error:
        raise DicomDecodeError(
            "Unable to decode DICOM pixel data. The transfer syntax may "
            "require an optional pydicom decoder such as pylibjpeg."
        ) from error


def decode_dicom_frames(dataset: Dataset) -> tuple[np.ndarray, ...]:
    """Decode every frame in ``dataset`` to display-ready uint8 pixels."""

    raw_frames = split_dicom_frames(
        _decode_pixel_array(dataset), get_dicom_frame_count(dataset)
    )
    return tuple(prepare_dicom_frame(frame, dataset) for frame in raw_frames)


def _write_png(path: os.PathLike | str, frame: np.ndarray) -> None:
    try:
        Image.fromarray(frame).save(os.fspath(path), format="PNG")
    except Exception as error:
        raise DicomDecodeError(
            f"Unable to write extracted PNG frame: {path}"
        ) from error


def extract_dataset_frames_to_png(
    dataset: Dataset,
    output_dir: os.PathLike | str,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> DicomExtractionResult:
    """Materialize a validated DICOM dataset as a lossless PNG sequence."""

    destination = osp.abspath(os.fspath(output_dir))
    os.makedirs(destination, exist_ok=True)
    raw_frames = split_dicom_frames(
        _decode_pixel_array(dataset), get_dicom_frame_count(dataset)
    )
    total = len(raw_frames)
    frame_paths = []
    cancelled = False

    for index, raw_frame in enumerate(raw_frames):
        if is_cancelled is not None and is_cancelled():
            cancelled = True
            break
        frame = prepare_dicom_frame(raw_frame, dataset)
        frame_path = osp.join(destination, f"frame_{index:06d}.png")
        _write_png(frame_path, frame)
        frame_paths.append(frame_path)
        if progress_callback is not None:
            progress_callback(index + 1, total)

    return DicomExtractionResult(
        output_dir=destination,
        frame_paths=tuple(frame_paths),
        fps=get_dicom_fps(dataset),
        cancelled=cancelled,
    )


def extract_dicom_frames_to_png(
    source_path: os.PathLike | str,
    output_dir: os.PathLike | str,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> DicomExtractionResult:
    """Read a DICOM/RUN file and materialize its frames as PNG files."""

    dataset = load_dicom_dataset(source_path)
    return extract_dataset_frames_to_png(
        dataset,
        output_dir,
        progress_callback=progress_callback,
        is_cancelled=is_cancelled,
    )


def get_output_directory(source_path: os.PathLike | str) -> str:
    """Return the sibling ``<source-stem>_frames`` directory."""

    source = Path(os.fspath(source_path))
    return str(source.with_name(f"{source.stem}_frames"))


def _translate(parent, text: str) -> str:
    translator = getattr(parent, "tr", None)
    return translator(text) if callable(translator) else text


def _confirm_overwrite(parent, output_dir: str) -> bool:
    if not osp.exists(output_dir):
        return True

    message_box = QMessageBox(parent)
    message_box.setIcon(QMessageBox.Icon.Warning)
    message_box.setWindowTitle(_translate(parent, "Warning"))
    message_box.setText(_translate(parent, "Directory Already Exists"))
    template = _translate(
        parent,
        "Directory '{}' already exists. Do you want to overwrite it?",
    )
    message_box.setInformativeText(template.format(osp.basename(output_dir)))
    message_box.setStandardButtons(
        QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok
    )
    message_box.setDefaultButton(QMessageBox.StandardButton.Ok)
    message_box.setStyleSheet(get_msg_box_style())
    return message_box.exec() == QMessageBox.StandardButton.Ok


def _apply_playback_fps(parent, fps: float) -> None:
    setter = getattr(parent, "set_playback_fps", None)
    if callable(setter):
        setter(fps)
        return

    playback_bar = getattr(parent, "playback_control_bar", None)
    setter = getattr(playback_bar, "set_fps", None)
    if callable(setter):
        setter(fps)
        return

    setter = getattr(parent, "set_fps", None)
    if callable(setter):
        setter(fps)
        return

    parent.media_fps = fps


def open_dicom_file(parent) -> str | None:
    """Open a DICOM image run, extract PNG frames, and import the sequence."""

    may_continue = getattr(parent, "may_continue", None)
    if callable(may_continue) and not may_continue():
        return None

    filename = getattr(parent, "filename", None)
    default_path = (
        osp.dirname(str(filename))
        if filename
        else getattr(parent, "last_open_dir", None) or "."
    )
    file_filter = (
        "DICOM / RUN Files (*.dcm *.dicom *.ima *.run);;All Files (*)"
    )
    source_path, _ = QFileDialog.getOpenFileName(
        parent,
        _translate(parent, "Open DICOM / RUN file"),
        default_path,
        file_filter,
    )
    if not source_path:
        return None

    output_dir = get_output_directory(source_path)
    temporary_dir = None
    progress_dialog = None
    try:
        dataset = load_dicom_dataset(source_path)
        if osp.lexists(output_dir) and (
            not osp.isdir(output_dir) or osp.islink(output_dir)
        ):
            raise DicomReadError(
                "The DICOM frame output path already exists and is not "
                f"a directory: {output_dir}"
            )
        if not _confirm_overwrite(parent, output_dir):
            return None

        frame_count = get_dicom_frame_count(dataset)
        progress_dialog = QProgressDialog(
            _translate(parent, "Extracting DICOM frames..."),
            _translate(parent, "Cancel"),
            0,
            frame_count,
            parent,
        )
        progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        progress_dialog.setWindowTitle(_translate(parent, "Progress"))
        progress_dialog.setMinimumWidth(400)
        progress_dialog.setMinimumHeight(150)
        progress_dialog.setStyleSheet(
            get_progress_dialog_style(color="#1d1d1f", height=20)
        )
        progress_dialog.setValue(0)
        progress_dialog.show()
        QApplication.processEvents()

        output_parent = osp.dirname(output_dir) or "."
        temporary_dir = tempfile.mkdtemp(
            prefix=f".{osp.basename(output_dir)}-",
            dir=output_parent,
        )

        def update_progress(completed: int, total: int) -> None:
            progress_dialog.setMaximum(total)
            progress_dialog.setValue(completed)
            QApplication.processEvents()

        result = extract_dataset_frames_to_png(
            dataset,
            temporary_dir,
            progress_callback=update_progress,
            is_cancelled=progress_dialog.wasCanceled,
        )
        if result.cancelled:
            logger.info("DICOM frame extraction was cancelled.")
            return None
        if not result.frame_paths:
            raise DicomDecodeError(
                "The DICOM object contains no image frames."
            )

        progress_dialog.close()
        progress_dialog = None
        install_extracted_directory(temporary_dir, output_dir)
        temporary_dir = None

        _apply_playback_fps(parent, result.fps)
        parent.import_image_folder(output_dir)
        logger.info(
            f"Extracted {len(result.frame_paths)} DICOM frames to "
            f"{output_dir} at {result.fps:.3f} FPS"
        )
        return output_dir
    except DicomError as error:
        logger.error(f"Failed to open DICOM/RUN file: {error}")
        QMessageBox.critical(
            parent,
            _translate(parent, "DICOM / RUN Error"),
            str(error),
        )
    except Exception as error:
        logger.exception("Unexpected DICOM/RUN import error")
        QMessageBox.critical(
            parent,
            _translate(parent, "DICOM / RUN Error"),
            _translate(parent, "Failed to open DICOM / RUN file: {}").format(
                error
            ),
        )
    finally:
        if progress_dialog is not None:
            progress_dialog.close()
        if temporary_dir is not None and osp.exists(temporary_dir):
            shutil.rmtree(temporary_dir, ignore_errors=True)
    return None
