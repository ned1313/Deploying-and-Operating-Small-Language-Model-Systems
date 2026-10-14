"""Tools test wiring. The harness tests drive the real complaint API in process, so they reuse the
app test fixtures; tools code is imported from tools/, the API from app/."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
APP_DIR = TOOLS_DIR.parent / "app"
sys.path[:0] = [str(TOOLS_DIR), str(APP_DIR)]

# Both suites are packages named `tests`, so load the app conftest by path and re-export its fixtures.
_spec = importlib.util.spec_from_file_location("app_tests_conftest", APP_DIR / "tests" / "conftest.py")
_app_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_app_conftest)  # type: ignore[union-attr]
globals().update({name: value for name, value in vars(_app_conftest).items() if not name.startswith("_")})
