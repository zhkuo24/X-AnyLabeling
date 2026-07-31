"""Shared helpers for safely replacing extracted media frame directories."""

import os
import os.path as osp
import shutil
import tempfile
from pathlib import Path


PRESERVED_SIDECAR_EXTENSIONS = frozenset(
    {
        ".csv",
        ".enc",
        ".json",
        ".jsonl",
        ".npy",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


class MediaDirectoryInstallError(RuntimeError):
    """Raised when new frames and the previous directory cannot be restored."""


def _suffix(path) -> str:
    return Path(os.fspath(path)).suffix.lower()


def _copy_preserved_sidecars(source_dir: str, destination_dir: str) -> None:
    """Carry annotation sidecars into a freshly extracted frame directory."""

    if not osp.isdir(source_dir):
        return

    for root, _dirs, filenames in os.walk(source_dir):
        relative_root = osp.relpath(root, source_dir)
        destination_root = (
            destination_dir
            if relative_root == "."
            else osp.join(destination_dir, relative_root)
        )
        for filename in filenames:
            if _suffix(filename) not in PRESERVED_SIDECAR_EXTENSIONS:
                continue
            os.makedirs(destination_root, exist_ok=True)
            shutil.copy2(
                osp.join(root, filename),
                osp.join(destination_root, filename),
            )


def install_extracted_directory(temporary_dir: str, output_dir: str) -> None:
    """Atomically install new frames while preserving existing annotations."""

    if not osp.lexists(output_dir):
        os.replace(temporary_dir, output_dir)
        return
    if not osp.isdir(output_dir) or osp.islink(output_dir):
        raise NotADirectoryError(
            f"Frame output path exists but is not a directory: {output_dir}"
        )

    _copy_preserved_sidecars(output_dir, temporary_dir)
    output_parent = osp.dirname(output_dir) or "."
    backup_root = tempfile.mkdtemp(
        prefix=f".{osp.basename(output_dir)}-backup-",
        dir=output_parent,
    )
    backup_path = osp.join(backup_root, "previous")
    try:
        os.replace(output_dir, backup_path)
    except Exception:
        shutil.rmtree(backup_root, ignore_errors=True)
        raise

    installed = False
    try:
        os.replace(temporary_dir, output_dir)
    except Exception:
        try:
            os.replace(backup_path, output_dir)
        except Exception as rollback_error:
            raise MediaDirectoryInstallError(
                "Unable to install the extracted frames or restore the "
                f"previous directory. The previous data remains at "
                f"'{backup_path}'."
            ) from rollback_error
        raise
    else:
        installed = True
    finally:
        if (installed or not osp.lexists(backup_path)) and osp.exists(
            backup_root
        ):
            shutil.rmtree(backup_root, ignore_errors=True)
