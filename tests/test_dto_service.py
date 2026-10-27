from datetime import date

import boto3
import pytest
from botocore.stub import ANY, Stubber

from dto_tui import (
    INTER_AZ,
    INTER_REGION,
    INTERNET_OUT,
    NAT_GATEWAY,
    OTHER_OUT,
    TO_CLOUDFRONT,
    Cache,
    CostExplorer,
    DtoQueryError,
    DtoService,
    MockCostExplorerClient,
    Option,
    classify,
    next_dimension,
    parse_filters,
)

TODAY = date(2026, 10, 26)


@pytest.mark.parametrize(
    "usage_type,expected",
    [
        ("DataTransfer-Out-Bytes", INTERNET_OUT),
        ("USW2-DataTransfer-Out-Bytes", INTERNET_OUT),
        ("US-DataTransfer-Out-Bytes", INTERNET_OUT),
        ("USE1-USW2-AWS-Out-Bytes", INTER_REGION),
        ("EU-EUC1-AWS-Out-ABytes", INTER_REGION),
        ("DataTransfer-Regional-Bytes", INTER_AZ),
        ("APS1-DataTransfer-Regional-Bytes", INTER_AZ),
        ("NatGateway-Bytes", NAT_GATEWAY),
        ("USE2-NatGateway-Bytes", NAT_GATEWAY),
        ("USE1-CloudFront-Out-Bytes", TO_CLOUDFRONT),
        ("US-DataTransfer-Out-OBytes", OTHER_OUT),
        ("USE1-DataTransfer-In-Bytes", None),
        ("USE1-USW2-AWS-In-Bytes", None),
        ("USE1-BoxUsage:t3.micro", None),
    ],
)
def test_classify(usage_type, expected):
    assert classify(usage_type) == expected


def test_parse_filters():
    assert parse_filters(["REGION=us-east-1", "REGION=eu-west-1", "TAG:vpc=", "COST_CATEGORY:BU=Core"]) == {
        "REGION": ["us-east-1", "eu-west-1"],
        "TAG:vpc": [""],
        "COST_CATEGORY:BU": ["Core"],
    }
    with pytest.raises(DtoQueryError):
        parse_filters(["REGION"])
    with pytest.raises(DtoQueryError):
        parse_filters(["AZ=us-east-1a"])


def test_summary_with_stubbed_cost_explorer():
    client = boto3.client("ce", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    stub = Stubber(client)

    # Discovery: one call per search string.
    found = {
        "DataTransfer": ["USE1-DataTransfer-Out-Bytes", "USE1-DataTransfer-In-Bytes", "USE1-DataTransfer-Regional-Bytes"],
        "Out-Bytes": ["USE1-DataTransfer-Out-Bytes", "USE1-USW2-AWS-Out-Bytes"],
        "Out-OBytes": [],
        "Out-ABytes": [],
        "NatGateway-Bytes": [],
    }
    for values in found.values():
        stub.add_response(
            "get_dimension_values",
            {"DimensionValues": [{"Value": v, "Attributes": {}} for v in values], "ReturnSize": len(values), "TotalSize": len(values)},
            {"TimePeriod": ANY, "Dimension": "USAGE_TYPE", "Context": "COST_AND_USAGE", "SearchString": ANY},
        )

    def m(cost: float, gb: float) -> dict:
        return {"UnblendedCost": {"Amount": str(cost), "Unit": "USD"}, "UsageQuantity": {"Amount": str(gb), "Unit": "GB"}}

    stub.add_response(
        "get_cost_and_usage",
        {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-09-01", "End": "2026-09-02"},
                    "Total": {},
                    "Groups": [
                        {"Keys": ["USE1-DataTransfer-Out-Bytes", "us-east-1"], "Metrics": m(9.0, 100)},
                        {"Keys": ["USE1-USW2-AWS-Out-Bytes", "us-east-1"], "Metrics": m(1.0, 50)},
                    ],
                    "Estimated": False,
                },
                {
                    "TimePeriod": {"Start": "2026-09-02", "End": "2026-09-03"},
                    "Total": {},
                    "Groups": [{"Keys": ["USE1-DataTransfer-Regional-Bytes", "eu-west-1"], "Metrics": m(10.0, 1000)}],
                    "Estimated": True,
                },
            ]
        },
        {
            "TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost", "UsageQuantity"],
            "Filter": {
                "And": [
                    {
                        "Dimensions": {
                            "Key": "USAGE_TYPE",
                            "Values": ["USE1-DataTransfer-Out-Bytes", "USE1-DataTransfer-Regional-Bytes", "USE1-USW2-AWS-Out-Bytes"],
                        }
                    },
                    {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
                    {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": ["111111111111"]}},
                ]
            },
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}, {"Type": "DIMENSION", "Key": "REGION"}],
        },
    )
    # Previous period: discovery again (different window), then totals.
    for values in found.values():
        stub.add_response(
            "get_dimension_values",
            {"DimensionValues": [{"Value": v, "Attributes": {}} for v in values], "ReturnSize": len(values), "TotalSize": len(values)},
            {"TimePeriod": ANY, "Dimension": "USAGE_TYPE", "Context": "COST_AND_USAGE", "SearchString": ANY},
        )
    stub.add_response(
        "get_cost_and_usage",
        {"ResultsByTime": [{"TimePeriod": {"Start": "2026-08-01", "End": "2026-08-31"}, "Total": m(15.0, 900), "Groups": []}]},
        {"TimePeriod": {"Start": "2026-08-01", "End": "2026-08-31"}, "Granularity": "MONTHLY", "Metrics": ANY, "Filter": ANY},
    )

    with stub:
        svc = DtoService(CostExplorer(client), Cache(0))
        s = svc.summary("month", date(2026, 9, 15), None, "REGION", {"LINKED_ACCOUNT": ["111111111111"]}, today=TODAY)
        stub.assert_no_pending_responses()

    assert s.total_cost == pytest.approx(20.0)
    assert s.total_gb == pytest.approx(1150)
    assert s.prev_total_cost == pytest.approx(15.0)
    assert s.estimated is True
    assert [r.key for r in s.rows] == ["us-east-1", "eu-west-1"]
    assert s.rows[0].label == "US East (N. Virginia)"
    assert s.rows[0].by_type == {INTERNET_OUT: 9.0, INTER_REGION: 1.0}
    assert s.rows[0].pct == pytest.approx(50.0)
    assert s.by_type[INTER_AZ].gb == pytest.approx(1000)
    assert [p.date for p in s.series] == [date(2026, 9, 1), date(2026, 9, 2)]
    assert [p.values for p in s.volume_series] == [{INTERNET_OUT: 100, INTER_REGION: 50}, {INTER_AZ: 1000}]


@pytest.fixture
def mock_service() -> DtoService:
    return DtoService(CostExplorer(MockCostExplorerClient()), Cache(0), mock=True)


@pytest.mark.parametrize("group_by", ["REGION", "LINKED_ACCOUNT", "SERVICE", "TRANSFER_TYPE", "USAGE_TYPE", "TAG:vpc", "COST_CATEGORY:Business Unit"])
def test_group_by_totals_are_consistent(mock_service, group_by):
    s = mock_service.summary("month", date(2026, 9, 1), None, group_by, {}, today=TODAY)
    assert s.total_cost > 0
    assert sum(r.cost for r in s.rows) == pytest.approx(s.total_cost)
    assert sum(a.cost for a in s.by_type.values()) == pytest.approx(s.total_cost)
    assert sum(sum(p.values.values()) for p in s.volume_series) == pytest.approx(s.total_gb)
    assert all(not r.key.endswith("In-Bytes") for r in s.rows)


def test_filters_narrow_results(mock_service):
    full = mock_service.summary("week", date(2026, 10, 12), None, "REGION", {}, today=TODAY)
    filtered = mock_service.summary(
        "week", date(2026, 10, 12), None, "REGION", {"REGION": ["eu-west-1"], "TRANSFER_TYPE": [INTER_AZ]}, today=TODAY
    )
    assert [r.key for r in filtered.rows] == ["eu-west-1"]
    assert set(filtered.rows[0].by_type) == {INTER_AZ}
    assert 0 < filtered.total_cost < full.total_cost

    untagged = mock_service.summary("week", date(2026, 10, 12), None, "TAG:vpc", {"TAG:vpc": [""]}, today=TODAY)
    assert [r.label for r in untagged.rows] == ["(untagged)"]


def test_meta(mock_service):
    meta = mock_service.meta(today=TODAY)
    assert Option(value="111111111111", label="prod") in meta.accounts
    assert "TAG:vpc" in [o.value for o in meta.group_by_options]
    assert "COST_CATEGORY:Business Unit" in [o.value for o in meta.group_by_options]


@pytest.mark.parametrize(
    "group_by,expected",
    [
        ("REGION", "LINKED_ACCOUNT"),
        ("LINKED_ACCOUNT", "TRANSFER_TYPE"),
        ("TRANSFER_TYPE", "USAGE_TYPE"),
        ("USAGE_TYPE", "LINKED_ACCOUNT"),
        ("COST_CATEGORY:Business Unit", "LINKED_ACCOUNT"),
        ("SERVICE", "TRANSFER_TYPE"),
        ("TAG:vpc", "TRANSFER_TYPE"),
    ],
)
def test_next_dimension(group_by, expected):
    assert next_dimension(group_by) == expected


def test_discounts_excluded_by_default(mock_service):
    gross = mock_service.summary("month", date(2026, 9, 1), None, "REGION", {}, today=TODAY)
    net = mock_service.summary("month", date(2026, 9, 1), None, "REGION", {}, include_discounts=True, today=TODAY)

    assert all(r.cost >= 0 for r in gross.rows)
    assert sum(r.pct for r in gross.rows) == pytest.approx(100)
    assert net.include_discounts and not gross.include_discounts
    assert net.total_cost < gross.total_cost
    assert net.total_gb == pytest.approx(gross.total_gb)  # discounts carry no volume
    assert any(r.cost < 0 for r in net.rows)
