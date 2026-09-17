"""Make ``src`` importable for the test suite.

Mirrors the path setup ``run.py`` performs for the CLI. Without this, tests
depend on collection order: only a few modules insert ``src`` themselves, so
modules collected before the first of those fail to import ``yt_auto``.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
