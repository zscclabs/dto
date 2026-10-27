import asyncio
import csv
from datetime import date

from dto_tui import Cache, CostExplorer, DtoApp, DtoService, MockCostExplorerClient, ViewState, parse_args


def _app(**state) -> DtoApp:
    service = DtoService(CostExplorer(MockCostExplorerClient()), Cache(3600), mock=True)
    return DtoApp(service, ViewState(**state), tag_key="vpc")


def test_app_drill_down_and_export(tmp_path, monkeypatch):
    """Drive the TUI headlessly in mock mode: load, drill down and back, export CSV."""
    monkeypatch.chdir(tmp_path)

    async def run() -> None:
        app = _app()
        async with app.run_test(size=(160, 50)) as pilot:

            async def settle() -> None:
                await app.workers.wait_for_complete()
                await pilot.pause()

            await settle()
            assert app.state.group_by == "TAG:vpc"  # --tag-key preselects the tag breakdown
            assert app.query_one("#table").row_count == len(app.summary.rows) > 0

            await pilot.press("enter")
            await settle()
            assert app.state.group_by == "TRANSFER_TYPE"
            assert list(app.state.filters) == ["TAG:vpc"]

            await pilot.press("backspace")
            await settle()
            assert app.state.group_by == "TAG:vpc" and app.state.filters == {}

            await pilot.press("e")
            await settle()

    asyncio.run(run())
    [path] = tmp_path.glob("dto_*_TAG-vpc.csv")
    header = next(csv.reader(path.open()))
    assert header[:5] == ["TAG:vpc", "label", "cost_usd", "gb", "pct"]


def test_app_period_navigation_and_discounts():
    async def run() -> None:
        app = _app(period="week", anchor=date(2026, 10, 14))
        async with app.run_test(size=(160, 50)) as pilot:

            async def settle() -> None:
                await app.workers.wait_for_complete()
                await pilot.pause()

            await settle()
            assert app.summary.window.start == date(2026, 10, 12)
            await pilot.press("left_square_bracket")
            await settle()
            assert app.summary.window.start == date(2026, 10, 5)

            gross = app.summary.total_cost
            app.query_one("#discounts").value = True
            await settle()
            assert app.summary.include_discounts and app.summary.total_cost < gross

    asyncio.run(run())


def test_parse_args():
    args = parse_args(["--mock", "--period", "custom", "--start", "2026-07-01", "--end", "2026-09-30", "--filter", "REGION=us-east-1"])
    assert args.mock and args.period == "custom"
    assert args.filters == {"REGION": ["us-east-1"]}
