#!/usr/bin/env python3
"""Decrypt .xalenc label files. See xlabel-decrypt -h."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from anylabeling.label_crypto import decrypt_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(decrypt_main())
