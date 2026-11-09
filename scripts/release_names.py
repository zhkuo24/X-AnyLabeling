#!/usr/bin/env python3
"""Filenames for the Windows and macOS packages published from main.

``anylabeling/app_info.py`` keeps a single application version until a
person edits it. The download name adds that version and the first 8
hex digits of the source commit, so two builds of one application
version do not share a filename.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

SHORT_SHA_LENGTH = 8
ROOT_DIR = Path(__file__).resolve().parent.parent
VERSION_PATTERN = re.compile(
    r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE
)
VERSION_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]*\Z")
SHA_TOKEN = re.compile(r"[0-9a-f]{8,40}\Z")
DIGEST_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
CHECKSUM_LINE = re.compile(r"([0-9a-fA-F]{64})  (\S+)\Z")
MACOS_ARCHS = frozenset({"arm64", "x86_64", "universal2"})


def parse_app_version(text: str) -> str:
    match = VERSION_PATTERN.search(text)
    if not match:
        raise ValueError("app_info.py has no __version__ assignment")
    version = match.group(1)
    if not VERSION_TOKEN.fullmatch(version):
        raise ValueError(f"unsupported version token: {version!r}")
    return version


def read_app_version(root: Path | None = None) -> str:
    app_info = (root or ROOT_DIR) / "anylabeling" / "app_info.py"
    return parse_app_version(app_info.read_text(encoding="utf-8"))


def short_sha(source: str) -> str:
    sha = source.strip().lower()
    if not SHA_TOKEN.fullmatch(sha):
        raise ValueError(f"source is not a git SHA: {source!r}")
    return sha[:SHORT_SHA_LENGTH]


def windows_cpu_executable(version: str, source: str) -> str:
    return f"X-AnyLabeling-v{version}-{short_sha(source)}-Windows-x64-CPU.exe"


def macos_unsigned_zip(version: str, source: str, arch: str = "arm64") -> str:
    if arch not in MACOS_ARCHS:
        raise ValueError(f"unsupported macOS arch: {arch!r}")
    return (
        f"X-AnyLabeling-v{version}-{short_sha(source)}"
        f"-macOS-{arch}-unsigned.zip"
    )


def checksum_filename(asset_name: str) -> str:
    return f"{asset_name}.sha256"


def format_checksum(asset_name: str, digest: str) -> str:
    normalized = digest.strip().lower()
    if not DIGEST_TOKEN.fullmatch(normalized):
        raise ValueError(f"digest is not sha256: {digest!r}")
    if not asset_name or any(character.isspace() for character in asset_name):
        raise ValueError(f"asset name is not a single token: {asset_name!r}")
    return f"{normalized}  {asset_name}\n"


def parse_checksum(text: str) -> tuple[str, str]:
    line = text.strip().replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in line:
        raise ValueError("checksum file must contain one line")
    match = CHECKSUM_LINE.fullmatch(line)
    if not match:
        raise ValueError(f"checksum line does not match sha256sum: {text!r}")
    return match.group(1).lower(), match.group(2)


def verify_checksum(asset_path: Path, checksum_path: Path) -> str:
    digest, name = parse_checksum(checksum_path.read_text(encoding="utf-8"))
    if name != asset_path.name:
        raise ValueError(
            f"checksum names {name}, package file is {asset_path.name}"
        )
    expected_checksum = checksum_filename(asset_path.name)
    if checksum_path.name != expected_checksum:
        raise ValueError(
            f"checksum file is {checksum_path.name}, "
            f"expected {expected_checksum}"
        )
    actual = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    if actual != digest:
        raise ValueError("checksum does not match the package bytes")
    return digest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "app-version",
            "short-sha",
            "windows-cpu-exe",
            "macos-zip",
            "format-checksum",
            "verify-checksum",
        ),
    )
    parser.add_argument("--source")
    parser.add_argument("--arch", default="arm64")
    parser.add_argument("--asset")
    parser.add_argument("--digest")
    parser.add_argument("--checksum")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "app-version":
        sys.stdout.write(read_app_version() + "\n")
        return 0
    if args.command == "short-sha":
        if not args.source:
            raise SystemExit("--source is required")
        sys.stdout.write(short_sha(args.source) + "\n")
        return 0
    if args.command == "windows-cpu-exe":
        if not args.source:
            raise SystemExit("--source is required")
        sys.stdout.write(
            windows_cpu_executable(read_app_version(), args.source) + "\n"
        )
        return 0
    if args.command == "macos-zip":
        if not args.source:
            raise SystemExit("--source is required")
        sys.stdout.write(
            macos_unsigned_zip(read_app_version(), args.source, args.arch)
            + "\n"
        )
        return 0
    if args.command == "format-checksum":
        if not args.asset or not args.digest:
            raise SystemExit("--asset and --digest are required")
        sys.stdout.write(format_checksum(args.asset, args.digest))
        return 0
    if not args.asset or not args.checksum:
        raise SystemExit("--asset and --checksum are required")
    verify_checksum(Path(args.asset), Path(args.checksum))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
