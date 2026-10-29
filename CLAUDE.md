# DTO TUI

Single-file Textual TUI (`dto_tui.py`) for AWS data transfer volume via Cost Explorer. Ported from the DTO Explorer web app (github.com/zscclabs/dto-explorer, local checkout `~/toil/dto`), whose `backend/app/*.py` are the source for the core sections.

- Keep it one file, runnable with `uv run dto_tui.py`; dependencies go in both the PEP 723 header and `pyproject.toml`.
- Core logic (top of the file) must stay free of UI concerns; the Textual UI is the bottom section.
- Results must match DTO Explorer: if classification, filter or summary logic changes in one repo, port it to the other.
- Version: `pyproject.toml` `version` and `__version__` in `dto_tui.py` must match; update `CHANGELOG.md` (Keep a Changelog).
- Tests: `uv run pytest -q`. UI tests drive the app headlessly with `App.run_test()` in mock mode. Try the app with `uv run dto_tui.py --mock --tag-key vpc`.
- Every Cost Explorer request costs $0.01; never add calls in loops without caching.
