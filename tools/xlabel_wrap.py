#!/usr/bin/env python3
"""Wrap a data key into an annotator .xalk credential. See xlabel-wrap -h."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from anylabeling.label_crypto import wrap_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(wrap_main())
