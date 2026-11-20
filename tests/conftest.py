from __future__ import annotations

import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
# The unit's scripts are imported by module name, as they import each other.
sys.path[:0] = [str(_TESTS.parent / "scripts"), str(_TESTS)]
