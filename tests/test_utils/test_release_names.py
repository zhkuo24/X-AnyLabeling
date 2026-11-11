import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_names.py"
sys.path.insert(0, str(ROOT / "scripts"))

from release_names import (  # noqa: E402
    checksum_filename,
    format_checksum,
    macos_unsigned_zip,
    parse_app_version,
    parse_checksum,
    read_app_version,
    short_sha,
    verify_checksum,
    windows_cpu_executable,
)

SOURCE = "04424979c417ac324f5363a9684470443df2878c"
OTHER = "ca48816643c2074faa1e69503df33c9c51e7b146"
VERSION = "4.0.0-beta.16"
WINDOWS = "X-AnyLabeling-v4.0.0-beta.16-04424979-Windows-x64-CPU.exe"
MACOS = "X-AnyLabeling-v4.0.0-beta.16-04424979-macOS-arm64-unsigned.zip"


class TestReleaseNames(unittest.TestCase):
    def test_app_version_stays_the_checked_in_value(self):
        text = (ROOT / "anylabeling" / "app_info.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(parse_app_version(text), VERSION)
        self.assertEqual(read_app_version(ROOT), VERSION)
        from anylabeling.app_info import __version__ as app_version
        from anylabeling.app import __version__ as cli_version
        from anylabeling.views.labeling.widgets.about_dialog import (
            __version__ as about_version,
        )
        from anylabeling.views.mainwindow import __version__ as window_version

        self.assertEqual(app_version, VERSION)
        self.assertIs(cli_version, app_version)
        self.assertIs(about_version, app_version)
        self.assertIs(window_version, app_version)

    def test_short_sha_is_eight_lowercase_hex_digits(self):
        self.assertEqual(short_sha(SOURCE), "04424979")
        self.assertEqual(short_sha(SOURCE.upper()), "04424979")
        self.assertEqual(short_sha("04424979"), "04424979")
        with self.assertRaises(ValueError):
            short_sha("0442497")
        with self.assertRaises(ValueError):
            short_sha("0442497g")

    def test_platform_names_include_version_and_source(self):
        self.assertEqual(windows_cpu_executable(VERSION, SOURCE), WINDOWS)
        self.assertEqual(macos_unsigned_zip(VERSION, SOURCE, "arm64"), MACOS)
        other = windows_cpu_executable(VERSION, OTHER)
        self.assertNotEqual(other, WINDOWS)
        self.assertIn("ca488166", other)
        self.assertIn(VERSION, other)

    def test_checksum_names_the_published_file(self):
        digest = "ab" * 32
        text = format_checksum(WINDOWS, digest.upper())
        parsed_digest, parsed_name = parse_checksum(text)
        self.assertEqual(parsed_digest, digest)
        self.assertEqual(parsed_name, WINDOWS)
        self.assertEqual(checksum_filename(WINDOWS), WINDOWS + ".sha256")
        old_digest, old_name = parse_checksum(
            f"{digest}  X-AnyLabeling-Windows-x64-CPU.exe\n"
        )
        self.assertEqual(old_digest, digest)
        self.assertNotEqual(old_name, WINDOWS)

    def test_verify_checksum_matches_bytes_and_filename(self):
        payload = b"package-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / WINDOWS
            checksum = Path(directory) / checksum_filename(WINDOWS)
            asset.write_bytes(payload)
            checksum.write_text(
                format_checksum(WINDOWS, digest), encoding="utf-8"
            )
            self.assertEqual(verify_checksum(asset, checksum), digest)
            checksum.write_text(
                format_checksum("X-AnyLabeling-Windows-x64-CPU.exe", digest),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                verify_checksum(asset, checksum)

    def test_cli_matches_the_library(self):
        windows = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "windows-cpu-exe",
                "--source",
                SOURCE,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        macos = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "macos-zip",
                "--source",
                SOURCE,
                "--arch",
                "arm64",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(windows.stdout, WINDOWS + "\n")
        self.assertEqual(macos.stdout, MACOS + "\n")

    def test_packaging_and_publish_use_the_same_names(self):
        build = (ROOT / "scripts" / "build_executable.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("windows-cpu-exe", build)
        self.assertIn("macos-zip", build)
        windows_build = (
            ROOT / ".github" / "workflows" / "windows-build.yml"
        ).read_text(encoding="utf-8")
        macos_build = (
            ROOT / ".github" / "workflows" / "macos-build.yml"
        ).read_text(encoding="utf-8")
        windows_release = (
            ROOT / ".github" / "workflows" / "windows-release.yml"
        ).read_text(encoding="utf-8")
        macos_release = (
            ROOT / ".github" / "workflows" / "macos-release.yml"
        ).read_text(encoding="utf-8")
        for text in (
            windows_build,
            macos_build,
            windows_release,
            macos_release,
        ):
            self.assertIn("scripts/release_names.py", text)
            self.assertNotIn(
                "release-assets/X-AnyLabeling-Windows-x64-CPU.exe", text
            )
            self.assertNotIn("X-AnyLabeling-macOS-arm64-unsigned.zip)", text)
        self.assertIn("X_ANYLABELING_SOURCE", windows_build)
        self.assertIn("X_ANYLABELING_SOURCE", macos_build)
        self.assertIn("windows-cpu-exe", windows_release)
        self.assertIn("macos-zip", macos_release)
        docs = (ROOT / "docs" / "zh_cn" / "get_started.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("<8位源码SHA>", docs)
        self.assertIn("releases/tag/windows-latest", docs)
        self.assertIn("releases/tag/macos-latest", docs)
        english = (ROOT / "docs" / "en" / "get_started.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("<8-char-source-SHA>", english)
        self.assertIn("releases/tag/windows-latest", english)
        self.assertIn("releases/tag/macos-latest", english)


if __name__ == "__main__":
    unittest.main()
