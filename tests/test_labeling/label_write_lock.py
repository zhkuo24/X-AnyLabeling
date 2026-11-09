"""Block LabelFile.save without deleting the assertions that watch it.

POSIX directory mode 0555 makes tempfile.mkstemp fail. Windows
os.chmod only toggles the read-only attribute and still lets a process
create a file in that directory, which is why the same chmod left the
Windows CI run with no PermissionError and no save-error dialog.
A read-only destination makes os.replace fail there instead.
"""

import os
import stat
from pathlib import Path


def deny_label_writes(directory):
    directory = Path(directory)
    if os.name == "nt":
        for path in directory.glob("*.json"):
            os.chmod(path, stat.S_IREAD)
        return
    os.chmod(directory, 0o555)


def allow_label_writes(directory):
    directory = Path(directory)
    os.chmod(directory, 0o755)
    for path in directory.glob("*"):
        if path.is_file():
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass
