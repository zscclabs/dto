from datetime import date

import boto3
import pytest
from botocore.stub import ANY, Stubber

from dto_tui import (
    AWS_INTERNAL_TYPES,
    INTER_AZ,
    INTER_REGION_IN,
    INTER_REGION_OUT,
    INTERNET_IN,
    INTERNET_OUT,
    NAT_GATEWAY,
    OTHER_IN,
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
        ("USE1-USW2-AWS-Out-Bytes", INTER_REGION_OUT),
        ("EU-EUC1-AWS-Out-ABytes", INTER_REGION_OUT),
        ("DataTransfer-Regional-Bytes", INTER_AZ),
        ("APS1-DataTransfer-Regional-Bytes", INTER_AZ),
        ("NatGateway-Bytes", NAT_GATEWAY),
        ("USE2-NatGateway-Bytes", NAT_GATEWAY),
        ("USE1-CloudFront-Out-Bytes", TO_CLOUDFRONT),
        ("US-DataTransfer-Out-OBytes", OTHER_OUT),
        ("USE1-DataTransfer-In-Bytes", INTERNET_IN),
        ("DataTransfer-In-Bytes", INTERNET_IN),
        ("USE1-USW2-AWS-In-Bytes", INTER_REGION_IN),
        ("US-DataTransfer-In-OBytes", OTHER_IN),
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
        "In-Bytes": ["USE1-DataTransfer-In-Bytes"],
        "In-OBytes": [],
        "In-ABytes": [],
        "NatGateway-Bytes": [],
    }
    for values in found.values():
        stub.add_response(
            "get_dimension_values",
            {"DimensionValues": [{"Value": v, "Attributes": {}} for v in values], "ReturnSize": len(values), "TotalSize": len(values)},
            {"TimePeriod": ANY, "Dimension": "USAGE_TYPE", "Context": "COST_AND_USAGE", "SearchString": ANY},
        )

    def m(gb: float) -> dict:
        return {"UsageQuantity": {"Amount": str(gb), "Unit": "GB"}}

    stub.add_response(
        "get_cost_and_usage",
        {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-09-01", "End": "2026-09-02"},
                    "Total": {},
                    "Groups": [
                        {"Keys": ["USE1-DataTransfer-Out-Bytes", "us-east-1"], "Metrics": m(100)},
                        {"Keys": ["USE1-USW2-AWS-Out-Bytes", "us-east-1"], "Metrics": m(50)},
                        {"Keys": ["USE1-DataTransfer-In-Bytes", "us-east-1"], "Metrics": m(200)},
                    ],
                    "Estimated": False,
                },
                {
                    "TimePeriod": {"Start": "2026-09-02", "End": "2026-09-03"},
                    "Total": {},
                    "Groups": [{"Keys": ["USE1-DataTransfer-Regional-Bytes", "eu-west-1"], "Metrics": m(1000)}],
                    "Estimated": True,
                },
            ]
        },
        {
            "TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"},
            "Granularity": "DAILY",
            "Metrics": ["UsageQuantity"],
            "Filter": {
                "And": [
                    {
                        "Dimensions": {
                            "Key": "USAGE_TYPE",
                            "Values": [
                                "USE1-DataTransfer-In-Bytes",
                                "USE1-DataTransfer-Out-Bytes",
                                "USE1-DataTransfer-Regional-Bytes",
                                "USE1-USW2-AWS-Out-Bytes",
                            ],
                        }
                    },
                    {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
                    {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": ["111111111111"]}},
                ]
            },
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}, {"Type": "DIMENSION", "Key": "REGION"}],
        },
    )
    # Previous period: discovery again (different window), then GB per usage type.
    for values in found.values():
        stub.add_response(
            "get_dimension_values",
            {"DimensionValues": [{"Value": v, "Attributes": {}} for v in values], "ReturnSize": len(values), "TotalSize": len(values)},
            {"TimePeriod": ANY, "Dimension": "USAGE_TYPE", "Context": "COST_AND_USAGE", "SearchString": ANY},
        )
    stub.add_response(
        "get_cost_and_usage",
        {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-31"},
                    "Total": {},
                    "Groups": [
                        {"Keys": ["USE1-DataTransfer-Out-Bytes"], "Metrics": m(100)},
                        {"Keys": ["USE1-DataTransfer-In-Bytes"], "Metrics": m(40)},
                        {"Keys": ["USE1-DataTransfer-Regional-Bytes"], "Metrics": m(800)},
                    ],
                }
            ]
        },
        {
            "TimePeriod": {"Start": "2026-08-01", "End": "2026-08-31"},
            "Granularity": "MONTHLY",
            "Metrics": ["UsageQuantity"],
            "Filter": ANY,
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        },
    )

    with stub:
        svc = DtoService(CostExplorer(client), Cache(0))
        s = svc.summary("month", date(2026, 9, 15), None, "REGION", {"LINKED_ACCOUNT": ["111111111111"]}, include_aws_internal=True, today=TODAY)
        stub.assert_no_pending_responses()

    assert s.total_gb == pytest.approx(1350)
    assert (s.egress_gb, s.ingress_gb, s.transit_gb) == pytest.approx((150, 200, 1000))
    assert s.prev_total_gb == pytest.approx(940)
    assert (s.prev_egress_gb, s.prev_ingress_gb, s.prev_transit_gb) == pytest.approx((100, 40, 800))
    assert s.estimated is True
    assert [r.key for r in s.rows] == ["eu-west-1", "us-east-1"]
    assert s.rows[1].label == "US East (N. Virginia)"
    assert s.rows[1].by_type == {INTERNET_OUT: 100, INTER_REGION_OUT: 50, INTERNET_IN: 200}
    assert s.rows[1].pct == pytest.approx(350 / 1350 * 100)
    assert s.by_type[INTER_AZ] == pytest.approx(1000)
    assert [p.date for p in s.series] == [date(2026, 9, 1), date(2026, 9, 2)]
    assert [p.values for p in s.volume_series] == [{INTERNET_OUT: 100, INTER_REGION_OUT: 50, INTERNET_IN: 200}, {INTER_AZ: 1000}]


@pytest.fixture
def mock_service() -> DtoService:
    return DtoService(CostExplorer(MockCostExplorerClient()), Cache(0), mock=True)


@pytest.mark.parametrize("group_by", ["REGION", "LINKED_ACCOUNT", "SERVICE", "TRANSFER_TYPE", "DIRECTION", "USAGE_TYPE", "TAG:vpc", "COST_CATEGORY:Business Unit"])
def test_group_by_totals_are_consistent(mock_service, group_by):
    s = mock_service.summary("month", date(2026, 9, 1), None, group_by, {}, today=TODAY)
    assert s.total_gb > 0
    assert sum(r.gb for r in s.rows) == pytest.approx(s.total_gb)
    assert sum(r.pct for r in s.rows) == pytest.approx(100)
    assert sum(s.by_type.values()) == pytest.approx(s.total_gb)
    assert sum(sum(p.values.values()) for p in s.series) == pytest.approx(s.total_gb)
    assert sum(sum(p.values.values()) for p in s.volume_series) == pytest.approx(s.total_gb)
    assert s.egress_gb + s.ingress_gb + s.transit_gb == pytest.approx(s.total_gb)


def test_filters_narrow_results(mock_service):
    full = mock_service.summary("week", date(2026, 10, 12), None, "REGION", {}, include_aws_internal=True, today=TODAY)
    filtered = mock_service.summary(
        "week", date(2026, 10, 12), None, "REGION", {"REGION": ["eu-west-1"], "TRANSFER_TYPE": [INTER_AZ]}, include_aws_internal=True, today=TODAY
    )
    assert [r.key for r in filtered.rows] == ["eu-west-1"]
    assert set(filtered.rows[0].by_type) == {INTER_AZ}
    assert 0 < filtered.total_gb < full.total_gb

    untagged = mock_service.summary("week", date(2026, 10, 12), None, "TAG:vpc", {"TAG:vpc": [""]}, today=TODAY)
    assert [r.label for r in untagged.rows] == ["(untagged)"]


def test_meta(mock_service):
    meta = mock_service.meta(today=TODAY)
    assert Option(value="111111111111", label="prod") in meta.accounts
    assert "TAG:vpc" in [o.value for o in meta.group_by_options]
    assert "COST_CATEGORY:Business Unit" in [o.value for o in meta.group_by_options]
    assert meta.directions[INTERNET_IN] == "In" and meta.directions[NAT_GATEWAY] == "Transit"


@pytest.mark.parametrize(
    "group_by,expected",
    [
        ("REGION", "LINKED_ACCOUNT"),
        ("LINKED_ACCOUNT", "TRANSFER_TYPE"),
        ("DIRECTION", "TRANSFER_TYPE"),
        ("TRANSFER_TYPE", "USAGE_TYPE"),
        ("USAGE_TYPE", "LINKED_ACCOUNT"),
        ("COST_CATEGORY:Business Unit", "LINKED_ACCOUNT"),
        ("SERVICE", "TRANSFER_TYPE"),
        ("TAG:vpc", "TRANSFER_TYPE"),
    ],
)
def test_next_dimension(group_by, expected):
    assert next_dimension(group_by) == expected


def test_inbound_counted(mock_service):
    s = mock_service.summary("month", date(2026, 9, 1), None, "DIRECTION", {}, include_aws_internal=True, today=TODAY)
    assert {r.key for r in s.rows} == {"Out", "In", "Transit"}
    assert [r.gb for r in s.rows] == sorted((r.gb for r in s.rows), reverse=True)
    assert s.ingress_gb == pytest.approx(s.by_type[INTERNET_IN] + s.by_type[INTER_REGION_IN] + s.by_type[OTHER_IN])
    assert s.by_type[INTERNET_IN] > 0 and s.by_type[INTER_REGION_IN] > 0
    assert 0 < s.prev_ingress_gb


def test_direction_filter(mock_service):
    s = mock_service.summary("month", date(2026, 9, 1), None, "TRANSFER_TYPE", {"DIRECTION": ["In"]}, include_aws_internal=True, today=TODAY)
    assert {r.key for r in s.rows} == {INTERNET_IN, INTER_REGION_IN}
    assert s.total_gb == pytest.approx(s.ingress_gb)
    assert s.egress_gb == 0 and s.transit_gb == 0


def test_nat_volume_excluded_by_default(mock_service):
    s = mock_service.summary("month", date(2026, 9, 1), None, "REGION", {}, today=TODAY)
    with_nat = mock_service.summary("month", date(2026, 9, 1), None, "REGION", {}, include_nat_volume=True, today=TODAY)

    assert not s.include_nat_volume and with_nat.include_nat_volume
    assert s.by_type[NAT_GATEWAY] == 0
    assert all(NAT_GATEWAY not in p.values for p in s.volume_series)
    assert with_nat.by_type[NAT_GATEWAY] > 0
    assert with_nat.transit_gb == pytest.approx(s.transit_gb + with_nat.by_type[NAT_GATEWAY])
    assert with_nat.total_gb == pytest.approx(s.total_gb + with_nat.by_type[NAT_GATEWAY])
    assert (with_nat.egress_gb, with_nat.ingress_gb) == pytest.approx((s.egress_gb, s.ingress_gb))
    assert with_nat.prev_transit_gb > s.prev_transit_gb
    assert with_nat.prev_egress_gb == pytest.approx(s.prev_egress_gb)


def test_internet_only_by_default(mock_service):
    s = mock_service.summary("month", date(2026, 9, 1), None, "TRANSFER_TYPE", {}, today=TODAY)
    full = mock_service.summary("month", date(2026, 9, 1), None, "TRANSFER_TYPE", {}, include_aws_internal=True, today=TODAY)

    assert not s.include_aws_internal and full.include_aws_internal
    assert {r.key for r in s.rows} == {INTERNET_OUT, INTERNET_IN}
    assert all(s.by_type[t] == 0 for t in AWS_INTERNAL_TYPES)
    assert s.transit_gb == 0 and s.prev_transit_gb == 0
    assert s.egress_gb == pytest.approx(full.by_type[INTERNET_OUT] + full.by_type[OTHER_OUT])
    assert s.ingress_gb == pytest.approx(full.by_type[INTERNET_IN] + full.by_type[OTHER_IN])
    assert s.prev_egress_gb < full.prev_egress_gb
    assert all(full.by_type[t] > 0 for t in AWS_INTERNAL_TYPES)

    # NAT keeps its own switch: with internal traffic hidden, transit is NAT only.
    nat = mock_service.summary("month", date(2026, 9, 1), None, "TRANSFER_TYPE", {}, include_nat_volume=True, today=TODAY)
    assert nat.transit_gb == pytest.approx(nat.by_type[NAT_GATEWAY]) and nat.transit_gb > 0
