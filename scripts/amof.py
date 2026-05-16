#!/usr/bin/env python3
"""AMOF CLI wrapper.

Historical launcher kept for compatibility. The shared implementation lives in
``amof.entrypoint`` so the script launcher, installed ``amof`` console script,
and ``python -m amof`` all execute the same code path.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_AMOF_SCRIPTS = ROOT / "repos" / "amof" / "scripts"
if RUNTIME_AMOF_SCRIPTS.exists():
    sys.path.insert(0, str(RUNTIME_AMOF_SCRIPTS))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from amof.entrypoint import main


if __name__ == "__main__":
    main()
