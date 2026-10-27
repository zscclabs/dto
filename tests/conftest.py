"""dto_tui.py is a script, not a package; load it as a module for the tests."""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("dto_tui", Path(__file__).resolve().parents[1] / "dto_tui.py")
dto_tui = importlib.util.module_from_spec(_spec)
sys.modules["dto_tui"] = dto_tui
_spec.loader.exec_module(dto_tui)
