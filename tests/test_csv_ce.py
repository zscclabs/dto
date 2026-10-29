import asyncio
import csv
from datetime import date, timedelta

import pytest

from dto_tui import (
    INTERNET_IN,
    INTERNET_OUT,
    Cache,
    CostExplorer,
    CsvCostExplorerClient,
    CsvDataError,
    DtoApp,
    DtoService,
    MockCostExplorerClient,
    ViewState,
    parse_args,
)

TODAY = date(2026, 10, 26)

FULL = """\
date,linked_account,linked_account_name,usage_type,region,service,tag:vpc,cost_category:Team,usage_quantity
2026-09-01,111111111111,prod,USE1-DataTransfer-Out-Bytes,us-east-1,EC2 - Other,vpc-a,Core,10
2026-09-01,111111111111,prod,USE1-DataTransfer-Out-Bytes,us-east-1,EC2 - Other,vpc-a,Core,5
2026-09-02,222222222222,staging,EU-DataTransfer-Out-Bytes,eu-west-1,EC2 - Other,,Edge,20
2026-09-03,111111111111,prod,USE1-DataTransfer-In-Bytes,us-east-1,EC2 - Other,vpc-b,Core,7
2026-09-03,111111111111,prod,USE1-DataTransfer-Regional-Bytes,us-east-1,EC2 - Other,vpc-a,Core,100
2026-09-03,111111111111,prod,USE1-BoxUsage:t3.micro,us-east-1,EC2,vpc-a,Core,24
2026-08-15,111111111111,prod,USE1-DataTransfer-Out-Bytes,us-east-1,EC2 - Other,vpc-a,Core,3
"""
MINIMAL = "Date,Usage_Type,Usage_Quantity\n2026-09-01,USE1-DataTransfer-Out-Bytes,4\n\n"


def write(path, text):
    path.write_text(text)
    return path


def service(path) -> DtoService:
    client = CsvCostExplorerClient(path)
    return DtoService(CostExplorer(client), Cache(0), source="csv", dimensions=client.dimensions)


@pytest.fixture
def full(tmp_path) -> DtoService:
    return service(write(tmp_path / "usage.csv", FULL))


def test_totals(full):
    s = full.summary("month", date(2026, 9, 1), None, "REGION", {}, today=TODAY)
    assert s.source == "csv" and not s.mock
    assert s.egress_gb == pytest.approx(35)
    assert s.ingress_gb == pytest.approx(7)
    assert s.transit_gb == 0  # Inter-AZ is hidden by default
    assert s.by_type[INTERNET_OUT] == pytest.approx(35)
    assert s.by_type[INTERNET_IN] == pytest.approx(7)
    assert s.prev_total_gb == pytest.approx(3)
    assert {r.key: r.gb for r in s.rows} == pytest.approx({"us-east-1": 22, "eu-west-1": 20})
    assert sum(sum(p.values.values()) for p in s.series) == pytest.approx(42)

    full_view = full.summary("month", date(2026, 9, 1), None, "REGION", {}, include_aws_internal=True, today=TODAY)
    assert full_view.transit_gb == pytest.approx(100)


def test_group_by_and_filters(full):
    s = full.summary("month", date(2026, 9, 1), None, "LINKED_ACCOUNT", {}, today=TODAY)
    assert {r.label: r.gb for r in s.rows} == pytest.approx({"prod": 22, "staging": 20})

    s = full.summary("month", date(2026, 9, 1), None, "TAG:vpc", {}, today=TODAY)
    assert {r.label: r.gb for r in s.rows} == pytest.approx({"vpc-a": 15, "vpc-b": 7, "(untagged)": 20})

    s = full.summary("month", date(2026, 9, 1), None, "COST_CATEGORY:Team", {"REGION": ["us-east-1"]}, today=TODAY)
    assert {r.key: r.gb for r in s.rows} == pytest.approx({"Core": 22})

    s = full.summary("month", date(2026, 9, 1), None, "REGION", {"TAG:vpc": [""]}, today=TODAY)
    assert s.total_gb == pytest.approx(20)


def test_meta(full):
    meta = full.meta(today=TODAY)
    assert meta.source == "csv"
    assert [a.label for a in meta.accounts] == ["prod", "staging"]
    assert [r.value for r in meta.regions] == ["eu-west-1", "us-east-1"]
    assert meta.tag_keys == ["vpc"]
    assert meta.cost_categories == ["Team"]
    assert {"REGION", "LINKED_ACCOUNT", "SERVICE", "TAG:vpc", "COST_CATEGORY:Team"} <= {o.value for o in meta.group_by_options}


def test_minimal_columns(tmp_path):
    svc = service(write(tmp_path / "min.csv", MINIMAL))
    options = {o.value for o in svc.meta(today=TODAY).group_by_options}
    assert options.isdisjoint({"REGION", "LINKED_ACCOUNT", "SERVICE"})
    assert {"TRANSFER_TYPE", "DIRECTION", "USAGE_TYPE"} <= options
    s = svc.summary("month", date(2026, 9, 1), None, "REGION", {}, today=TODAY)
    assert s.total_gb == pytest.approx(4)
    assert s.rows[0].label == "(none)"


def test_directory_and_reload(tmp_path):
    write(tmp_path / "a.csv", "date,usage_type,usage_quantity\n2026-09-01,USE1-DataTransfer-Out-Bytes,1\n")
    write(tmp_path / "b.csv", "date,usage_type,usage_quantity\n2026-09-02,USE1-DataTransfer-Out-Bytes,2\n")
    write(tmp_path / "notes.txt", "ignored")
    client = CsvCostExplorerClient(tmp_path)
    assert len(client.data.files) == 2 and client.data.rows == 2
    assert client.data.date_range == (date(2026, 9, 1), date(2026, 9, 2))

    write(tmp_path / "c.csv", "date,usage_type,usage_quantity\n2026-09-03,USE1-DataTransfer-Out-Bytes,3\n")
    client.reload()
    assert client.data.rows == 3

    write(tmp_path / "c.csv", "date,usage_type,usage_quantity\nbad,USE1-DataTransfer-Out-Bytes,3\n")
    with pytest.raises(CsvDataError):
        client.reload()
    assert client.data.rows == 3  # previous data kept


@pytest.mark.parametrize(
    "text,message",
    [
        ("date,usage_type\n2026-09-01,x\n", "usage.csv:1: missing required column(s): usage_quantity"),
        ("date,usage_type,usage_quantity\n2026/09/01,x,1\n", "usage.csv:2: date must be YYYY-MM-DD"),
        ("date,usage_type,usage_quantity\n2026-09-01,x,1\n2026-09-02,x,lots\n", "usage.csv:3: usage_quantity must be a number"),
        ("date,usage_type,usage_quantity\n2026-09-01,,1\n", "usage.csv:2: usage_type is empty"),
        ("", "usage.csv: empty file"),
    ],
)
def test_bad_csv(tmp_path, text, message):
    with pytest.raises(CsvDataError, match=message.replace("(", r"\(").replace(")", r"\)")):
        CsvCostExplorerClient(write(tmp_path / "usage.csv", text))


def test_missing_path(tmp_path):
    with pytest.raises(CsvDataError, match="no such file"):
        CsvCostExplorerClient(tmp_path / "nope.csv")
    with pytest.raises(CsvDataError, match="no .csv files"):
        CsvCostExplorerClient(tmp_path)


def test_matches_mock_round_trip(tmp_path):
    """Mock line items written to CSV give the same summary as the mock client."""
    mock = MockCostExplorerClient()
    start, end = date(2026, 9, 1), date(2026, 10, 1)
    path = tmp_path / "mock.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "linked_account", "usage_type", "region", "service", "record_type", "tag:vpc", "tag:team", "cost_category:Business Unit", "usage_quantity"])
        d = date(2026, 8, 1)
        while d < end:
            for line in mock.lines:
                gb = line.gb_on(d) if line.record_type == "Usage" else 0.0
                w.writerow([d, line.account, line.usage_type, line.region, line.service, line.record_type, line.tags["vpc"], line.tags["team"], line.cost_categories["Business Unit"], gb])
            d += timedelta(days=1)

    from_csv = service(path)
    from_mock = DtoService(CostExplorer(mock), Cache(0), source="mock")
    for group_by in ("REGION", "TAG:vpc", "COST_CATEGORY:Business Unit"):
        a = from_csv.summary("month", start, None, group_by, {"LINKED_ACCOUNT": ["111111111111"]}, include_aws_internal=True, today=TODAY)
        b = from_mock.summary("month", start, None, group_by, {"LINKED_ACCOUNT": ["111111111111"]}, include_aws_internal=True, today=TODAY)
        assert a.total_gb == pytest.approx(b.total_gb)
        assert a.prev_total_gb == pytest.approx(b.prev_total_gb)
        assert {r.key: r.gb for r in a.rows} == pytest.approx({r.key: r.gb for r in b.rows})


def test_app_csv_mode(tmp_path):
    """CSV indicator, breakdown fallback for missing columns, and refresh that keeps data when the files break."""
    path = write(tmp_path / "min.csv", MINIMAL)

    async def run() -> None:
        app = DtoApp(service(path), ViewState(anchor=date(2026, 9, 1)), tag_key="vpc")
        async with app.run_test(size=(160, 50)) as pilot:

            async def settle() -> None:
                await app.workers.wait_for_complete()
                await pilot.pause()

            await settle()
            assert app.sub_title.startswith("CSV data")
            assert app.state.group_by == "TRANSFER_TYPE"  # no region column
            assert app.summary.total_gb == pytest.approx(4)

            await pilot.press("f")
            await pilot.pause()
            assert [c.id for c in app.screen.query("SelectionList")] == ["f-DIRECTION", "f-TRANSFER_TYPE"]
            await pilot.press("escape")

            write(path, "date,usage_type,usage_quantity\n2026-09-01,USE1-DataTransfer-Out-Bytes,4\n2026-09-02,USE1-DataTransfer-Out-Bytes,6\n")
            await pilot.press("r")
            await settle()
            assert app.summary.total_gb == pytest.approx(10)

            write(path, "date,usage_type,usage_quantity\n2026-09-03,USE1-DataTransfer-Out-Bytes,oops\n")
            await pilot.press("r")
            await settle()
            assert app.summary.total_gb == pytest.approx(10)  # old data kept
            assert any("min.csv:2: usage_quantity must be a number" in str(n.message) for n in app._notifications)

    asyncio.run(run())


def test_parse_args_csv(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DTO_MOCK", raising=False)
    monkeypatch.delenv("DTO_CSV_PATH", raising=False)
    assert parse_args([]).csv is None
    assert parse_args(["--csv", str(tmp_path)]).csv == tmp_path

    monkeypatch.setenv("DTO_CSV_PATH", str(tmp_path))
    assert parse_args([]).csv == tmp_path

    with pytest.raises(SystemExit):
        parse_args(["--mock"])
    assert "--csv can't be combined with --mock" in capsys.readouterr().err
