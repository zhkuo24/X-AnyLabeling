import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    SecondaryCaptureImageStorage,
    generate_uid,
)

from anylabeling.views.labeling.utils.dicom import (
    DEFAULT_DICOM_FPS,
    DicomDecodeError,
    DicomReadError,
    decode_dicom_frames,
    extract_dataset_frames_to_png,
    get_dicom_fps,
    get_output_directory,
    is_dicom_file,
    load_dicom_dataset,
    prepare_dicom_frame,
    split_dicom_frames,
)
from anylabeling.views.labeling.utils.media import (
    install_extracted_directory,
)


def make_image_dataset(
    pixels,
    *,
    photometric="MONOCHROME2",
    frame_count=None,
):
    """Build a small uncompressed DICOM image dataset for tests."""

    array = np.asarray(pixels)
    file_meta = FileMetaDataset()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.ImplementationClassUID = generate_uid()

    dataset = FileDataset(
        None,
        {},
        file_meta=file_meta,
        preamble=b"\0" * 128,
    )
    dataset.SOPClassUID = file_meta.MediaStorageSOPClassUID
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    dataset.PhotometricInterpretation = photometric

    is_color = photometric == "RGB"
    dataset.SamplesPerPixel = 3 if is_color else 1
    if is_color:
        dataset.PlanarConfiguration = 0
        if frame_count is None:
            dataset.Rows, dataset.Columns = array.shape[:2]
        else:
            dataset.Rows, dataset.Columns = array.shape[1:3]
    else:
        dataset.Rows, dataset.Columns = array.shape[-2:]

    if frame_count is not None:
        dataset.NumberOfFrames = str(frame_count)

    dataset.BitsAllocated = array.dtype.itemsize * 8
    dataset.BitsStored = dataset.BitsAllocated
    dataset.HighBit = dataset.BitsStored - 1
    dataset.PixelRepresentation = int(
        np.issubdtype(array.dtype, np.signedinteger)
    )

    if dataset.BitsAllocated == 16:
        byte_order = "<i2" if dataset.PixelRepresentation else "<u2"
        array = array.astype(byte_order, copy=False)
    else:
        array = np.ascontiguousarray(array)
    dataset.PixelData = array.tobytes()
    return dataset


class TestDicomTiming(unittest.TestCase):

    def test_fps_metadata_priority(self):
        dataset = Dataset()
        dataset.RecommendedDisplayFrameRate = 24
        dataset.CineRate = 30
        dataset.FrameTime = 20

        self.assertEqual(get_dicom_fps(dataset), 24.0)

        del dataset.RecommendedDisplayFrameRate
        self.assertEqual(get_dicom_fps(dataset), 30.0)

        del dataset.CineRate
        self.assertEqual(get_dicom_fps(dataset), 50.0)

    def test_fps_uses_frame_time_vector_and_safe_default(self):
        dataset = Dataset()
        dataset.FrameTimeVector = [0, 40, 60]
        self.assertEqual(get_dicom_fps(dataset), 20.0)

        dataset.FrameTimeVector = 50
        self.assertEqual(get_dicom_fps(dataset), 20.0)

        dataset = Dataset()
        self.assertEqual(get_dicom_fps(dataset, default="bad"), 30.0)
        self.assertEqual(DEFAULT_DICOM_FPS, 30.0)


class TestDicomPixelConversion(unittest.TestCase):

    def test_split_uses_declared_frame_count_for_single_rgb(self):
        rgb = np.zeros((4, 5, 3), dtype=np.uint8)
        frames = split_dicom_frames(rgb, 1)

        self.assertEqual(len(frames), 1)
        np.testing.assert_array_equal(frames[0], rgb)

    def test_split_multiframe_and_reject_shape_mismatch(self):
        pixels = np.zeros((3, 4, 5), dtype=np.uint16)
        frames = split_dicom_frames(pixels, 3)
        self.assertEqual([frame.shape for frame in frames], [(4, 5)] * 3)

        with self.assertRaises(DicomDecodeError):
            split_dicom_frames(pixels, 2)

    def test_uint16_window_and_normalization(self):
        windowed_dataset = Dataset()
        windowed_dataset.PhotometricInterpretation = "MONOCHROME2"
        windowed_dataset.WindowCenter = 50.5
        windowed_dataset.WindowWidth = 101
        pixels = np.array([[0, 50, 100]], dtype=np.uint16)

        windowed = prepare_dicom_frame(pixels, windowed_dataset)
        np.testing.assert_array_equal(
            windowed,
            np.array([[0, 128, 255]], dtype=np.uint8),
        )

        normalized_dataset = Dataset()
        normalized_dataset.PhotometricInterpretation = "MONOCHROME2"
        normalized = prepare_dicom_frame(
            np.array([[100, 200, 300]], dtype=np.uint16),
            normalized_dataset,
        )
        np.testing.assert_array_equal(
            normalized,
            np.array([[0, 128, 255]], dtype=np.uint8),
        )

    def test_monochrome1_is_inverted(self):
        dataset = Dataset()
        dataset.PhotometricInterpretation = "MONOCHROME1"
        result = prepare_dicom_frame(
            np.array([[10, 20, 30]], dtype=np.uint16),
            dataset,
        )

        np.testing.assert_array_equal(
            result,
            np.array([[255, 127, 0]], dtype=np.uint8),
        )

    def test_multiframe_rgb_decoding(self):
        pixels = np.array(
            [
                [[[255, 0, 0], [0, 255, 0]]],
                [[[0, 0, 255], [255, 255, 255]]],
            ],
            dtype=np.uint8,
        )
        dataset = make_image_dataset(
            pixels,
            photometric="RGB",
            frame_count=2,
        )

        frames = decode_dicom_frames(dataset)

        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].shape, (1, 2, 3))
        np.testing.assert_array_equal(frames[0], pixels[0])
        np.testing.assert_array_equal(frames[1], pixels[1])

    def test_uint16_color_is_normalized_to_rgb_uint8(self):
        dataset = Dataset()
        dataset.PhotometricInterpretation = "RGB"
        dataset.SamplesPerPixel = 3
        pixels = np.array(
            [[[0, 512, 1024], [1024, 512, 0]]],
            dtype=np.uint16,
        )

        result = prepare_dicom_frame(pixels, dataset)

        self.assertEqual(result.dtype, np.uint8)
        self.assertEqual(result.shape, pixels.shape)
        self.assertEqual(int(result.min()), 0)
        self.assertEqual(int(result.max()), 255)


class TestDicomFileHandling(unittest.TestCase):

    def test_regular_dicom_is_accepted_with_run_or_no_extension(self):
        dataset = make_image_dataset(
            np.array([[0, 1], [2, 3]], dtype=np.uint16)
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            for filename in ("image.run", "image_without_extension"):
                path = Path(temp_dir, filename)
                dataset.save_as(path, write_like_original=False)

                loaded = load_dicom_dataset(path)

                self.assertEqual(int(loaded.Rows), 2)
                self.assertTrue(is_dicom_file(path))

    def test_proprietary_run_has_explicit_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "vendor.run")
            path.write_bytes(b"not a DICOM object")

            with self.assertRaisesRegex(
                DicomReadError,
                "Proprietary RUN containers require a vendor-specific decoder",
            ):
                load_dicom_dataset(path)
            self.assertFalse(is_dicom_file(path))

    def test_reimport_preserves_annotations_and_replaces_old_frames(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir, "cine_frames")
            output_dir.mkdir()
            (output_dir / "frame_000000.png").write_bytes(b"old frame")
            (output_dir / "stale.png").write_bytes(b"stale frame")
            (output_dir / "frame_000000.json").write_text(
                '{"shapes": []}', encoding="utf-8"
            )
            (output_dir / "notes.txt").write_text(
                "reviewed", encoding="utf-8"
            )

            extracted_dir = Path(temp_dir, "new-extraction")
            extracted_dir.mkdir()
            (extracted_dir / "frame_000000.png").write_bytes(b"new frame")

            install_extracted_directory(
                str(extracted_dir), str(output_dir)
            )

            self.assertEqual(
                (output_dir / "frame_000000.png").read_bytes(), b"new frame"
            )
            self.assertFalse((output_dir / "stale.png").exists())
            self.assertEqual(
                (output_dir / "frame_000000.json").read_text(
                    encoding="utf-8"
                ),
                '{"shapes": []}',
            )
            self.assertEqual(
                (output_dir / "notes.txt").read_text(encoding="utf-8"),
                "reviewed",
            )
            self.assertFalse(extracted_dir.exists())

    def test_frame_install_refuses_to_replace_a_regular_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir, "cine_frames")
            output_path.write_text("unrelated data", encoding="utf-8")
            extracted_dir = Path(temp_dir, "new-extraction")
            extracted_dir.mkdir()
            (extracted_dir / "frame_000000.png").write_bytes(b"new frame")

            with self.assertRaises(NotADirectoryError):
                install_extracted_directory(
                    str(extracted_dir), str(output_path)
                )

            self.assertEqual(
                output_path.read_text(encoding="utf-8"), "unrelated data"
            )
            self.assertTrue(extracted_dir.exists())

    def test_extracts_multiframe_png_sequence_and_reports_fps(self):
        pixels = np.array(
            [
                [[0, 100], [200, 300]],
                [[300, 200], [100, 0]],
            ],
            dtype=np.uint16,
        )
        dataset = make_image_dataset(pixels, frame_count=2)
        dataset.FrameTime = 40
        progress = mock.Mock()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir, "study_frames")
            result = extract_dataset_frames_to_png(
                dataset,
                output_dir,
                progress_callback=progress,
            )

            self.assertFalse(result.cancelled)
            self.assertEqual(result.fps, 25.0)
            self.assertEqual(
                [Path(path).name for path in result.frame_paths],
                ["frame_000000.png", "frame_000001.png"],
            )
            progress.assert_has_calls([mock.call(1, 2), mock.call(2, 2)])
            with Image.open(result.frame_paths[0]) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.mode, "L")
                np.testing.assert_array_equal(
                    np.asarray(image),
                    np.array([[0, 85], [170, 255]], dtype=np.uint8),
                )

    def test_extraction_can_be_cancelled_between_frames(self):
        pixels = np.arange(12, dtype=np.uint16).reshape(3, 2, 2)
        dataset = make_image_dataset(pixels, frame_count=3)
        is_cancelled = mock.Mock(side_effect=[False, True])

        with tempfile.TemporaryDirectory() as temp_dir:
            result = extract_dataset_frames_to_png(
                dataset,
                temp_dir,
                is_cancelled=is_cancelled,
            )

            self.assertTrue(result.cancelled)
            self.assertEqual(len(result.frame_paths), 1)
            self.assertTrue(os.path.isfile(result.frame_paths[0]))

    def test_output_directory_is_a_sibling(self):
        self.assertEqual(
            get_output_directory("/data/study.dcm"),
            "/data/study_frames",
        )


if __name__ == "__main__":
    unittest.main()
