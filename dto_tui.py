#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=1.0",
#     "boto3>=1.35",
# ]
# ///
"""DTO TUI: AWS data transfer volume in the terminal.

A self-contained port of the DTO Explorer backend (https://github.com/zscclabs/dto-explorer)
with a Textual UI. Run it with `uv run dto_tui.py` (add `--mock` for synthetic data, no AWS needed,
or `--csv PATH` to read usage exported with scripts/export-ce-csv.sh).
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import os
import random
import re
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Hashable, Literal, TypeVar

__version__ = "0.2.0"

# =====================================================================================
# Periods (dto-explorer backend/app/periods.py)
# =====================================================================================

PeriodKind = Literal["week", "month", "custom"]

# Above this many days, daily granularity produces unreadable charts and slow queries.
MAX_DAILY_DAYS = 92


@dataclass(frozen=True)
class Window:
    start: date  # inclusive
    end: date  # exclusive (Cost Explorer semantics)
    granularity: Literal["DAILY", "MONTHLY"]

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def is_empty(self) -> bool:
        return self.end <= self.start

    @property
    def last_day(self) -> date:
        """Inclusive end date, for display."""
        return self.end - timedelta(days=1)


@dataclass(frozen=True)
class ResolvedPeriod:
    display: Window  # the full period the user selected
    query: Window  # display window clipped to data that can exist (up to today)
    previous: Window  # same number of days immediately before, for comparison


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def _granularity(days: int) -> Literal["DAILY", "MONTHLY"]:
    return "DAILY" if days <= MAX_DAILY_DAYS else "MONTHLY"


def resolve(
    kind: PeriodKind,
    start: date | None = None,
    end: date | None = None,
    today: date | None = None,
) -> ResolvedPeriod:
    """week: ISO week containing `start`; month: calendar month containing `start`; custom: `start`..`end` inclusive."""
    today = today or date.today()

    if kind == "week":
        anchor = start or today
        p_start = anchor - timedelta(days=anchor.weekday())
        p_end = p_start + timedelta(days=7)
        prev_start = p_start - timedelta(days=7)
    elif kind == "month":
        anchor = start or today
        p_start = anchor.replace(day=1)
        p_end = _add_months(p_start, 1)
        prev_start = _add_months(p_start, -1)
    else:
        if start is None or end is None:
            raise ValueError("custom period requires both start and end")
        if end < start:
            raise ValueError("end must be on or after start")
        p_start, p_end = start, end + timedelta(days=1)
        prev_start = p_start - (p_end - p_start)

    gran = _granularity((p_end - p_start).days)
    display = Window(p_start, p_end, gran)

    # Cost Explorer has no data for the future; clip so partial periods compare fairly.
    q_end = min(p_end, today + timedelta(days=1))
    query = Window(p_start, max(q_end, p_start), gran)

    prev_end = prev_start + timedelta(days=query.days)
    previous = Window(prev_start, prev_end, _granularity(query.days))
    return ResolvedPeriod(display=display, query=query, previous=previous)


def presets(today: date | None = None) -> dict[str, tuple[date, date]]:
    """Custom range presets, as in the web UI's period selector (inclusive bounds)."""
    today = today or date.today()
    yesterday = today - timedelta(days=1)
    this_month = today.replace(day=1)
    return {
        "Last 7 days": (today - timedelta(days=7), yesterday),
        "Last 30 days": (today - timedelta(days=30), yesterday),
        "Last 90 days": (today - timedelta(days=90), yesterday),
        "Year to date": (today.replace(month=1, day=1), today),
        "Last 12 months": (_add_months(this_month, -12), this_month - timedelta(days=1)),
    }


# =====================================================================================
# Cache (dto-explorer backend/app/cache.py)
# =====================================================================================

T = TypeVar("T")


class Cache:
    """Thread-safe TTL cache keyed on arbitrary hashable request parameters."""

    def __init__(self, ttl_seconds: int, maxsize: int = 512):
        self._data: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.ttl = max(ttl_seconds, 1)
        self.maxsize = maxsize
        self.enabled = ttl_seconds > 0

    def get_or_set(self, key: Hashable, fn: Callable[[], T]) -> T:
        if not self.enabled:
            return fn()
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = fn()
        with self._lock:
            if len(self._data) >= self.maxsize:
                self._data = {k: v for k, v in self._data.items() if v[0] > now}
                if len(self._data) >= self.maxsize:
                    self._data.pop(next(iter(self._data)))
            self._data[key] = (now + self.ttl, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# =====================================================================================
# Cost Explorer client (dto-explorer backend/app/ce_client.py)
# =====================================================================================


def _period(start: date, end: date) -> dict[str, str]:
    return {"Start": start.isoformat(), "End": end.isoformat()}


class CostExplorer:
    """Thin wrapper over the boto3 Cost Explorer client that handles pagination."""

    def __init__(self, client: Any):
        self._client = client

    @property
    def client(self) -> Any:
        return self._client

    def dimension_values(self, dimension: str, start: date, end: date, search: str | None = None) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"TimePeriod": _period(start, end), "Dimension": dimension, "Context": "COST_AND_USAGE"}
        if search:
            kwargs["SearchString"] = search
        values: list[dict[str, Any]] = []
        while True:
            resp = self._client.get_dimension_values(**kwargs)
            values.extend(resp.get("DimensionValues", []))
            token = resp.get("NextPageToken")
            if not token:
                return values
            kwargs["NextPageToken"] = token

    def cost_and_usage(
        self,
        start: date,
        end: date,
        granularity: str,
        metrics: list[str],
        filter_expr: dict[str, Any] | None = None,
        group_by: list[dict[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Return (ResultsByTime merged across pages, {dimension value: description})."""
        kwargs: dict[str, Any] = {"TimePeriod": _period(start, end), "Granularity": granularity, "Metrics": metrics}
        if filter_expr:
            kwargs["Filter"] = filter_expr
        if group_by:
            kwargs["GroupBy"] = group_by

        # Pages split groups of the same time bucket, so merge by period start.
        by_start: dict[str, dict[str, Any]] = {}
        attrs: dict[str, str] = {}
        while True:
            resp = self._client.get_cost_and_usage(**kwargs)
            for r in resp.get("ResultsByTime", []):
                key = r["TimePeriod"]["Start"]
                if key in by_start:
                    by_start[key].setdefault("Groups", []).extend(r.get("Groups", []))
                else:
                    by_start[key] = {**r, "Groups": list(r.get("Groups", []))}
            for a in resp.get("DimensionValueAttributes", []):
                desc = a.get("Attributes", {}).get("description")
                if desc:
                    attrs[a["Value"]] = desc
            token = resp.get("NextPageToken")
            if not token:
                break
            kwargs["NextPageToken"] = token
        return [by_start[k] for k in sorted(by_start)], attrs

    def tag_keys(self, start: date, end: date) -> list[str]:
        return self._paged_names("get_tags", "Tags", start, end)

    def cost_category_names(self, start: date, end: date) -> list[str]:
        return self._paged_names("get_cost_categories", "CostCategoryNames", start, end)

    def _paged_names(self, method: str, field_name: str, start: date, end: date) -> list[str]:
        kwargs: dict[str, Any] = {"TimePeriod": _period(start, end)}
        names: list[str] = []
        while True:
            resp = getattr(self._client, method)(**kwargs)
            names.extend(resp.get(field_name, []))
            token = resp.get("NextPageToken")
            if not token:
                return sorted(set(names))
            kwargs["NextPageToken"] = token


def make_cost_explorer(mock: bool, profile: str | None, region: str = "us-east-1", csv_path: Path | None = None) -> CostExplorer:
    if mock:
        return CostExplorer(MockCostExplorerClient())
    if csv_path:
        return CostExplorer(CsvCostExplorerClient(csv_path))

    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    client = session.client("ce", region_name=region, config=Config(retries={"mode": "adaptive", "max_attempts": 10}))
    return CostExplorer(client)


# =====================================================================================
# Synthetic Cost Explorer for --mock (dto-explorer backend/app/mock_ce.py)
# =====================================================================================
# The filter and grouping helpers are shared with the CSV client: a line item is anything
# with attr(dim), tags and cost_categories.

MOCK_ACCOUNTS = {"111111111111": "prod", "222222222222": "staging", "333333333333": "data-platform", "444444444444": "shared-services"}
_MOCK_BU = {"111111111111": "Core", "222222222222": "Core", "333333333333": "Analytics", "444444444444": "Platform"}
_MOCK_REGIONS = {"us-east-1": "USE1", "us-west-2": "USW2", "eu-west-1": "EU", "eu-central-1": "EUC1", "ap-southeast-1": "APS1"}
_MOCK_PEER = {"us-east-1": "us-west-2", "us-west-2": "us-east-1", "eu-west-1": "eu-central-1", "eu-central-1": "eu-west-1", "ap-southeast-1": "us-east-1"}
_MOCK_VPCS = {"us-east-1": "vpc-core-use1", "us-west-2": "vpc-dr-usw2", "eu-west-1": "vpc-edge-euw1", "eu-central-1": "vpc-core-euc1", "ap-southeast-1": "vpc-edge-apse1"}
_MOCK_TEAMS = ["payments", "search", "ml", ""]
_MOCK_PRICE = {"internet": 0.09, "interregion": 0.02, "interaz": 0.01, "nat": 0.045, "in": 0.0}


@dataclass
class _Line:
    usage_type: str
    region: str
    account: str
    service: str
    base_gb: float
    price: float
    seed: int
    tags: dict[str, str] = field(default_factory=dict)
    record_type: str = "Usage"
    cost_categories: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.cost_categories:
            self.cost_categories = {"Business Unit": _MOCK_BU.get(self.account, "")}

    def gb_on(self, d: date) -> float:
        rng = random.Random(self.seed * 100_003 + d.toordinal())
        weekly = 1.0 - 0.25 * (d.weekday() >= 5)
        trend = 1.0 + 0.0015 * (d.toordinal() - date(2025, 1, 1).toordinal())
        return max(self.base_gb * weekly * trend * (0.8 + 0.4 * rng.random()) * (1 + 0.1 * math.sin(d.toordinal() / 9)), 0)

    def attr(self, dim: str) -> str:
        return {
            "USAGE_TYPE": self.usage_type,
            "REGION": self.region,
            "LINKED_ACCOUNT": self.account,
            "SERVICE": self.service,
            "RECORD_TYPE": self.record_type,
        }.get(dim, "")


def _mock_lines() -> list[_Line]:
    rng = random.Random(42)
    lines: list[_Line] = []
    for account in MOCK_ACCOUNTS:
        for region, prefix in _MOCK_REGIONS.items():
            if account == "222222222222" and region not in ("us-east-1", "eu-west-1"):
                continue
            scale = 3.0 if account == "111111111111" else 1.0
            kinds = [
                (f"{prefix}-DataTransfer-Out-Bytes", "internet", "EC2 - Other", 400),
                (f"{prefix}-DataTransfer-Out-Bytes", "internet", "Amazon Simple Storage Service", 250),
                (f"{prefix}-DataTransfer-Regional-Bytes", "interaz", "EC2 - Other", 900),
                (f"{prefix}-DataTransfer-Regional-Bytes", "interaz", "Amazon Relational Database Service", 200),
                (f"{prefix}-NatGateway-Bytes", "nat", "EC2 - Other", 350),
                (f"{prefix}-{_MOCK_REGIONS[_MOCK_PEER[region]]}-AWS-Out-Bytes", "interregion", "AWS Data Transfer", 300),
                (f"{_MOCK_REGIONS[_MOCK_PEER[region]]}-{prefix}-AWS-In-Bytes", "in", "AWS Data Transfer", 220),
                (f"{prefix}-CloudFront-Out-Bytes", "in", "EC2 - Other", 120),
                (f"{prefix}-DataTransfer-In-Bytes", "in", "EC2 - Other", 500),
            ]
            for ut, kind, service, base in kinds:
                tags = {"vpc": _MOCK_VPCS[region] if rng.random() > 0.15 else "", "team": rng.choice(_MOCK_TEAMS)}
                lines.append(_Line(ut, region, account, service, base * scale * (0.3 + rng.random()), _MOCK_PRICE[kind], len(lines), tags))
    # CloudFront edge egress (global, 2-letter prefixes).
    for prefix, base in (("US", 2500), ("EU", 1200), ("AP", 600)):
        lines.append(_Line(f"{prefix}-DataTransfer-Out-Bytes", "global", "111111111111", "Amazon CloudFront", base, 0.085, len(lines), {"vpc": "", "team": "search"}))
    # Negotiated discounts are booked as negative line items, often without the original region.
    for prefix in ("USE1", "EU"):
        lines.append(
            _Line(f"{prefix}-DataTransfer-Out-Bytes", "NoRegion", "111111111111", "EC2 - Other", 1500, -0.015, len(lines), {"vpc": "", "team": ""}, "Private Rate Card Discount")
        )
    return lines


def _mock_matches(line: Any, expr: dict[str, Any] | None) -> bool:
    if not expr:
        return True
    if "And" in expr:
        return all(_mock_matches(line, e) for e in expr["And"])
    if "Or" in expr:
        return any(_mock_matches(line, e) for e in expr["Or"])
    if "Not" in expr:
        return not _mock_matches(line, expr["Not"])
    if "Dimensions" in expr:
        d = expr["Dimensions"]
        return line.attr(d["Key"]) in d.get("Values", [])
    for kind, get in (("Tags", lambda k: line.tags.get(k, "")), ("CostCategories", lambda k: line.cost_categories.get(k, ""))):
        if kind in expr:
            value = get(expr[kind]["Key"])
            if "ABSENT" in expr[kind].get("MatchOptions", []):
                return value == ""
            return value in expr[kind].get("Values", [])
    return True


def _mock_group_key(line: Any, g: dict[str, str]) -> str:
    if g["Type"] == "TAG":
        return f"{g['Key']}${line.tags.get(g['Key'], '')}"
    if g["Type"] == "COST_CATEGORY":
        return f"{g['Key']}${line.cost_categories.get(g['Key'], '')}"
    return line.attr(g["Key"])


def _buckets(start: date, end: date, granularity: str) -> list[tuple[date, date]]:
    out = []
    cur = start
    while cur < end:
        nxt = cur + timedelta(days=1) if granularity == "DAILY" else _add_months(cur, 1)
        out.append((cur, min(nxt, end)))
        cur = nxt
    return out


def _mock_metrics(names: list[str], cost: float, gb: float) -> dict[str, dict[str, str]]:
    return {n: ({"Amount": f"{gb:.6f}", "Unit": "GB"} if n == "UsageQuantity" else {"Amount": f"{cost:.6f}", "Unit": "USD"}) for n in names}


class MockCostExplorerClient:
    """In-memory stand-in for the boto3 Cost Explorer client."""

    def __init__(self) -> None:
        self.lines = _mock_lines()

    def get_dimension_values(self, TimePeriod: dict, Dimension: str, SearchString: str | None = None, **_: Any) -> dict:
        values = sorted({line.attr(Dimension) for line in self.lines} - {""})
        if SearchString:
            values = [v for v in values if SearchString.lower() in v.lower()]
        out = [{"Value": v, "Attributes": {"description": MOCK_ACCOUNTS[v]} if Dimension == "LINKED_ACCOUNT" else {}} for v in values]
        return {"DimensionValues": out, "ReturnSize": len(out), "TotalSize": len(out)}

    def get_tags(self, TimePeriod: dict, **_: Any) -> dict:
        return {"Tags": ["team", "vpc"], "ReturnSize": 2, "TotalSize": 2}

    def get_cost_categories(self, TimePeriod: dict, **_: Any) -> dict:
        return {"CostCategoryNames": ["Business Unit"], "ReturnSize": 1, "TotalSize": 1}

    def get_cost_and_usage(
        self, TimePeriod: dict, Granularity: str, Metrics: list[str], Filter: dict | None = None, GroupBy: list[dict] | None = None, **_: Any
    ) -> dict:
        start, end = date.fromisoformat(TimePeriod["Start"]), date.fromisoformat(TimePeriod["End"])
        if end <= start:
            raise ValueError("Start must be before End")
        today = date.today()
        lines = [line for line in self.lines if _mock_matches(line, Filter)]
        results = []
        for b_start, b_end in _buckets(start, end, Granularity):
            groups: dict[tuple[str, ...], list[float]] = {}
            total = [0.0, 0.0]
            d = b_start
            while d < min(b_end, today + timedelta(days=1)):
                for line in lines:
                    gb = line.gb_on(d)
                    cost = gb * line.price
                    if line.record_type != "Usage":
                        gb = 0.0  # discounts carry cost but no usage quantity
                    total[0] += cost
                    total[1] += gb
                    if GroupBy:
                        acc = groups.setdefault(tuple(_mock_group_key(line, g) for g in GroupBy), [0.0, 0.0])
                        acc[0] += cost
                        acc[1] += gb
                d += timedelta(days=1)
            results.append(
                {
                    "TimePeriod": {"Start": b_start.isoformat(), "End": b_end.isoformat()},
                    "Total": {} if GroupBy else _mock_metrics(Metrics, *total),
                    "Groups": [{"Keys": list(k), "Metrics": _mock_metrics(Metrics, c, g)} for k, (c, g) in sorted(groups.items())],
                    "Estimated": b_end >= today,
                }
            )
        attrs = []
        if GroupBy and any(g["Key"] == "LINKED_ACCOUNT" for g in GroupBy):
            attrs = [{"Value": k, "Attributes": {"description": v}} for k, v in MOCK_ACCOUNTS.items()]
        return {"ResultsByTime": results, "DimensionValueAttributes": attrs, "GroupDefinitions": GroupBy or []}


# =====================================================================================
# Cost Explorer stand-in that reads CSV files for --csv (dto-explorer backend/app/csv_ce.py)
# =====================================================================================
# The CSV format is documented in docs/csv-data.md; scripts/export-ce-csv.sh produces it
# with the AWS CLI. Rows are aggregated in memory and get_cost_and_usage filters / groups
# them with the same helpers as the mock client.

CSV_REQUIRED = ("date", "usage_type", "usage_quantity")
# Optional CSV column -> Cost Explorer dimension.
CSV_OPTIONAL = {"region": "REGION", "linked_account": "LINKED_ACCOUNT", "service": "SERVICE", "record_type": "RECORD_TYPE"}
CSV_TAG_PREFIX = "tag:"
CSV_COST_CATEGORY_PREFIX = "cost_category:"


class CsvDataError(ValueError):
    """The CSV data is missing, unreadable or malformed."""


@dataclass
class CsvLine:
    day: date
    usage_type: str
    region: str
    account: str
    service: str
    record_type: str
    gb: float
    tags: dict[str, str] = field(default_factory=dict)
    cost_categories: dict[str, str] = field(default_factory=dict)

    def attr(self, dim: str) -> str:
        return {
            "USAGE_TYPE": self.usage_type,
            "REGION": self.region,
            "LINKED_ACCOUNT": self.account,
            "SERVICE": self.service,
            "RECORD_TYPE": self.record_type,
        }.get(dim, "")


@dataclass
class CsvData:
    files: list[Path]
    rows: int
    lines: list[CsvLine]  # aggregated, sorted by day
    dimensions: set[str]  # REGION / LINKED_ACCOUNT / SERVICE columns present
    tag_keys: list[str]
    cost_categories: list[str]
    account_names: dict[str, str]

    @property
    def date_range(self) -> tuple[date, date] | None:
        return (self.lines[0].day, self.lines[-1].day) if self.lines else None


def _csv_files(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(p for p in path.glob("*.csv") if p.is_file())
        if not files:
            raise CsvDataError(f"{path}: no .csv files in directory")
        return files
    if path.is_file():
        return [path]
    raise CsvDataError(f"{path}: no such file or directory")


def load_csv(path: Path) -> CsvData:
    """Read one CSV file, or every *.csv file in a directory, and aggregate identical rows."""
    files = _csv_files(path)
    totals: dict[tuple, float] = defaultdict(float)
    dimensions: set[str] = set()
    tag_keys: set[str] = set()
    categories: set[str] = set()
    names: dict[str, str] = {}
    rows = 0

    for f in files:
        try:
            with f.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.reader(fh)
                header = next(reader, None)
                if header is None:
                    raise CsvDataError(f"{f}: empty file")
                cols = [h.strip() for h in header]
                lower = [c.lower() for c in cols]
                missing = [c for c in CSV_REQUIRED if c not in lower]
                if missing:
                    raise CsvDataError(f"{f}:1: missing required column(s): {', '.join(missing)}")
                idx = {c: i for i, c in enumerate(lower)}
                tags = [(c[len(CSV_TAG_PREFIX):], i) for i, c in enumerate(cols) if c.lower().startswith(CSV_TAG_PREFIX)]
                cats = [(c[len(CSV_COST_CATEGORY_PREFIX):], i) for i, c in enumerate(cols) if c.lower().startswith(CSV_COST_CATEGORY_PREFIX)]
                dimensions |= {dim for col, dim in CSV_OPTIONAL.items() if col in idx and dim != "RECORD_TYPE"}
                tag_keys |= {k for k, _ in tags}
                categories |= {k for k, _ in cats}

                def get(row: list[str], col: str, default: str = "") -> str:
                    i = idx.get(col)
                    return row[i].strip() if i is not None and i < len(row) else default

                for row in reader:
                    if not any(cell.strip() for cell in row):
                        continue
                    where = f"{f}:{reader.line_num}"
                    try:
                        day = date.fromisoformat(get(row, "date"))
                    except ValueError:
                        raise CsvDataError(f"{where}: date must be YYYY-MM-DD, got {get(row, 'date')!r}") from None
                    try:
                        gb = float(get(row, "usage_quantity"))
                    except ValueError:
                        raise CsvDataError(f"{where}: usage_quantity must be a number, got {get(row, 'usage_quantity')!r}") from None
                    usage_type = get(row, "usage_type")
                    if not usage_type:
                        raise CsvDataError(f"{where}: usage_type is empty")
                    account = get(row, "linked_account")
                    if name := get(row, "linked_account_name"):
                        names[account] = name
                    key = (
                        day,
                        usage_type,
                        get(row, "region"),
                        account,
                        get(row, "service"),
                        get(row, "record_type") or "Usage",
                        tuple((k, row[i].strip() if i < len(row) else "") for k, i in tags),
                        tuple((k, row[i].strip() if i < len(row) else "") for k, i in cats),
                    )
                    totals[key] += gb
                    rows += 1
        except OSError as e:
            raise CsvDataError(f"{f}: {e}") from e
        except UnicodeDecodeError as e:
            raise CsvDataError(f"{f}: not UTF-8 text ({e})") from e

    lines = [
        CsvLine(day, ut, region, account, service, rt, gb, dict(tags), dict(cats))
        for (day, ut, region, account, service, rt, tags, cats), gb in totals.items()
    ]
    lines.sort(key=lambda line: line.day)
    return CsvData(files, rows, lines, dimensions, sorted(tag_keys), sorted(categories), names)


class CsvCostExplorerClient:
    """Answers the Cost Explorer calls DtoService makes from CSV rows.

    TimePeriod is ignored by the discovery calls (dimension values, tags, cost categories)
    so that the filters list everything in the files, whatever dates they cover.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = load_csv(self.path)

    def reload(self) -> None:
        """Re-read the files; on error the previously loaded data stays in place."""
        self.data = load_csv(self.path)

    @property
    def dimensions(self) -> set[str]:
        return self.data.dimensions

    def get_dimension_values(self, TimePeriod: dict, Dimension: str, SearchString: str | None = None, **_: Any) -> dict:
        data = self.data
        values = sorted({line.attr(Dimension) for line in data.lines} - {""})
        if SearchString:
            values = [v for v in values if SearchString.lower() in v.lower()]
        out = []
        for v in values:
            attrs = {"description": data.account_names[v]} if Dimension == "LINKED_ACCOUNT" and v in data.account_names else {}
            out.append({"Value": v, "Attributes": attrs})
        return {"DimensionValues": out, "ReturnSize": len(out), "TotalSize": len(out)}

    def get_tags(self, TimePeriod: dict, **_: Any) -> dict:
        keys = self.data.tag_keys
        return {"Tags": keys, "ReturnSize": len(keys), "TotalSize": len(keys)}

    def get_cost_categories(self, TimePeriod: dict, **_: Any) -> dict:
        names = self.data.cost_categories
        return {"CostCategoryNames": names, "ReturnSize": len(names), "TotalSize": len(names)}

    def get_cost_and_usage(
        self, TimePeriod: dict, Granularity: str, Metrics: list[str], Filter: dict | None = None, GroupBy: list[dict] | None = None, **_: Any
    ) -> dict:
        start, end = date.fromisoformat(TimePeriod["Start"]), date.fromisoformat(TimePeriod["End"])
        if end <= start:
            raise ValueError("Start must be before End")
        data = self.data
        buckets = _buckets(start, end, Granularity)
        starts = [b for b, _ in buckets]
        groups: list[dict[tuple[str, ...], float]] = [defaultdict(float) for _ in buckets]
        totals = [0.0] * len(buckets)

        lo = bisect.bisect_left(data.lines, start, key=lambda line: line.day)
        hi = bisect.bisect_left(data.lines, end, key=lambda line: line.day)
        for line in data.lines[lo:hi]:
            if not _mock_matches(line, Filter):
                continue
            i = bisect.bisect_right(starts, line.day) - 1
            totals[i] += line.gb
            if GroupBy:
                groups[i][tuple(_mock_group_key(line, g) for g in GroupBy)] += line.gb

        results = [
            {
                "TimePeriod": {"Start": b_start.isoformat(), "End": b_end.isoformat()},
                "Total": {} if GroupBy else _mock_metrics(Metrics, 0.0, totals[i]),
                "Groups": [{"Keys": list(k), "Metrics": _mock_metrics(Metrics, 0.0, gb)} for k, gb in sorted(groups[i].items())],
                "Estimated": False,
            }
            for i, (b_start, b_end) in enumerate(buckets)
        ]
        attrs = []
        if GroupBy and any(g["Key"] == "LINKED_ACCOUNT" for g in GroupBy):
            attrs = [{"Value": k, "Attributes": {"description": v}} for k, v in data.account_names.items()]
        return {"ResultsByTime": results, "DimensionValueAttributes": attrs, "GroupDefinitions": GroupBy or []}


# =====================================================================================
# Models (dto-explorer backend/app/models.py)
# =====================================================================================


@dataclass
class Row:
    key: str
    label: str
    gb: float
    pct: float  # share of total_gb
    by_type: dict[str, float]  # GB per transfer type


@dataclass
class SeriesPoint:
    date: date
    values: dict[str, float]  # row key or transfer type -> GB


@dataclass
class Summary:
    period: str
    window: Window
    data_window: Window | None  # portion of the window that can have data
    previous_window: Window | None
    group_by: str
    include_nat_volume: bool  # NAT Gateway GB counted (it duplicates the traffic it carries)
    include_aws_internal: bool  # Inter-AZ, Inter-Region and To CloudFront counted; otherwise internet traffic only
    total_gb: float = 0.0
    egress_gb: float = 0.0  # direction Out: leaving AWS or crossing regions
    ingress_gb: float = 0.0  # direction In: arriving from the internet or another region
    transit_gb: float = 0.0  # moving inside AWS (Inter-AZ, NAT Gateway, To CloudFront)
    prev_total_gb: float | None = None
    prev_egress_gb: float | None = None
    prev_ingress_gb: float | None = None
    prev_transit_gb: float | None = None
    by_type: dict[str, float] = field(default_factory=dict)  # GB per transfer type
    rows: list[Row] = field(default_factory=list)
    series: list[SeriesPoint] = field(default_factory=list)  # row key -> GB
    volume_series: list[SeriesPoint] = field(default_factory=list)  # transfer type -> GB
    estimated: bool = False
    mock: bool = False
    source: str = "aws"  # aws, mock or csv


@dataclass(frozen=True)
class Option:
    value: str
    label: str


@dataclass
class Meta:
    accounts: list[Option]
    regions: list[Option]
    services: list[str]
    tag_keys: list[str]
    cost_categories: list[str]
    transfer_types: list[str]
    directions: dict[str, str]  # transfer type -> Out, In or Transit
    aws_internal_types: list[str]  # hidden unless include_aws_internal
    group_by_options: list[Option]
    mock: bool
    source: str  # aws, mock or csv


# =====================================================================================
# Data transfer discovery, classification and aggregation (dto-explorer backend/app/dto_service.py)
# =====================================================================================

INTERNET_OUT = "Internet Out"
INTER_REGION_OUT = "Inter-Region Out"
OTHER_OUT = "Other Out"
INTERNET_IN = "Internet In"
INTER_REGION_IN = "Inter-Region In"
OTHER_IN = "Other In"
INTER_AZ = "Inter-AZ"
NAT_GATEWAY = "NAT Gateway"
TO_CLOUDFRONT = "To CloudFront"

OUT = "Out"
IN = "In"
TRANSIT = "Transit"
DIRECTIONS = [OUT, IN, TRANSIT]
# Out: bytes leaving AWS or crossing regions. In: bytes arriving from the internet or another region.
# Transit moves data inside AWS and its bytes reappear under another type: NAT data processing and origin
# fetches to CloudFront duplicate the traffic they carry, and Inter-AZ is billed on both the sending and the
# receiving side.
DIRECTION = {
    INTERNET_OUT: OUT,
    INTER_REGION_OUT: OUT,
    OTHER_OUT: OUT,
    INTERNET_IN: IN,
    INTER_REGION_IN: IN,
    OTHER_IN: IN,
    INTER_AZ: TRANSIT,
    NAT_GATEWAY: TRANSIT,
    TO_CLOUDFRONT: TRANSIT,
}
TRANSFER_TYPES = list(DIRECTION)
# Traffic that stays on AWS's network. Hidden unless asked for, so the default view is traffic to and from the internet.
# NAT Gateway has its own switch (counted_gb).
AWS_INTERNAL_TYPES = [INTER_AZ, INTER_REGION_OUT, INTER_REGION_IN, TO_CLOUDFRONT]

# Usage types are "<region prefix>-<kind>", the prefix is omitted for some us-east-1 usage.
_P = r"(?:[A-Z0-9]+-)?"
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"^{_P}DataTransfer-Regional-Bytes$"), INTER_AZ),
    (re.compile(rf"^{_P}NatGateway-Bytes$"), NAT_GATEWAY),
    (re.compile(r"^[A-Z0-9]+-[A-Z0-9]+-AWS-Out-A?Bytes$"), INTER_REGION_OUT),
    (re.compile(r"^[A-Z0-9]+-[A-Z0-9]+-AWS-In-A?Bytes$"), INTER_REGION_IN),
    (re.compile(rf"^{_P}CloudFront-Out-Bytes$"), TO_CLOUDFRONT),
    # Also matches CloudFront edge egress (e.g. "US-DataTransfer-Out-Bytes"), which is internet egress too.
    (re.compile(rf"^{_P}DataTransfer-Out-Bytes$"), INTERNET_OUT),
    (re.compile(rf"^{_P}DataTransfer-In-Bytes$"), INTERNET_IN),
]

# Substrings used to discover candidate usage types via GetDimensionValues.
_SEARCH_STRINGS = ["DataTransfer", "Out-Bytes", "Out-OBytes", "Out-ABytes", "In-Bytes", "In-OBytes", "In-ABytes", "NatGateway-Bytes"]

DIMENSIONS = {
    "REGION": "Region",
    "LINKED_ACCOUNT": "Account",
    "SERVICE": "Service",
    "TRANSFER_TYPE": "Transfer type",
    "DIRECTION": "Direction",
    "USAGE_TYPE": "Usage type",
}
# Dimensions a CSV export may leave out; the others are derived from the usage type.
OPTIONAL_DIMENSIONS = ("REGION", "LINKED_ACCOUNT", "SERVICE")
UNTAGGED = "(untagged)"
NO_VALUE = "(none)"

REGION_NAMES = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)",
    "ca-west-1": "Canada West (Calgary)",
    "sa-east-1": "South America (São Paulo)",
    "mx-central-1": "Mexico (Central)",
    "eu-west-1": "Europe (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-west-3": "Europe (Paris)",
    "eu-central-1": "Europe (Frankfurt)",
    "eu-central-2": "Europe (Zurich)",
    "eu-north-1": "Europe (Stockholm)",
    "eu-south-1": "Europe (Milan)",
    "eu-south-2": "Europe (Spain)",
    "me-south-1": "Middle East (Bahrain)",
    "me-central-1": "Middle East (UAE)",
    "il-central-1": "Israel (Tel Aviv)",
    "af-south-1": "Africa (Cape Town)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ap-southeast-5": "Asia Pacific (Malaysia)",
    "ap-southeast-7": "Asia Pacific (Thailand)",
    "global": "Global",
    "NoRegion": "No region",
}


class DtoQueryError(ValueError):
    """Invalid query parameters."""


def classify(usage_type: str) -> str | None:
    """Map a usage type to a transfer type, or None when it is not data transfer."""
    for pattern, kind in _RULES:
        if pattern.match(usage_type):
            return kind
    if any(s in usage_type for s in ("Out-Bytes", "Out-OBytes", "Out-ABytes")):
        return OTHER_OUT
    if any(s in usage_type for s in ("In-Bytes", "In-OBytes", "In-ABytes")):
        return OTHER_IN
    return None


def counted_gb(kind: str, gb: float, include_nat_volume: bool) -> float:
    """Volume to count for a group: NAT bytes are a processing fee on traffic billed elsewhere, so 0 unless asked for."""
    return gb if include_nat_volume or kind != NAT_GATEWAY else 0.0


def parse_filters(raw: list[str]) -> dict[str, list[str]]:
    """Parse repeated `DIM=value` strings into {DIM: [values]} (OR within, AND across)."""
    out: dict[str, list[str]] = defaultdict(list)
    for item in raw:
        dim, sep, value = item.partition("=")
        if not sep:
            raise DtoQueryError(f"filter must look like DIM=value: {item!r}")
        _validate_dimension(dim)
        out[dim].append(value)
    return dict(out)


def _validate_dimension(dim: str) -> None:
    if dim in DIMENSIONS:
        return
    prefix, _, name = dim.partition(":")
    if prefix in ("TAG", "COST_CATEGORY") and name:
        return
    raise DtoQueryError(f"unsupported dimension: {dim!r}")


def _split_group_value(value: str) -> str:
    """Tag and cost category group keys come back as 'key$value'."""
    return value.split("$", 1)[1] if "$" in value else value


def _keyed_filter(kind: str, key: str, values: list[str]) -> dict[str, Any]:
    """Tag / cost category filter where '' means 'not set'."""
    present = [v for v in values if v != ""]
    parts: list[dict[str, Any]] = []
    if present:
        parts.append({kind: {"Key": key, "Values": present}})
    if len(present) != len(values):
        parts.append({kind: {"Key": key, "MatchOptions": ["ABSENT"]}})
    return parts[0] if len(parts) == 1 else {"Or": parts}


def _and(parts: list[dict[str, Any]]) -> dict[str, Any]:
    return parts[0] if len(parts) == 1 else {"And": parts}


def _by_direction(by_type: dict[str, float]) -> tuple[float, float, float]:
    """(out, in, transit) GB from GB per transfer type."""
    totals: dict[str, float] = defaultdict(float)
    for kind, gb in by_type.items():
        totals[DIRECTION[kind]] += gb
    return totals[OUT], totals[IN], totals[TRANSIT]


def dim_label(dim: str) -> str:
    if dim in DIMENSIONS:
        return DIMENSIONS[dim]
    if dim.startswith("TAG:"):
        return f"Tag: {dim[4:]}"
    if dim.startswith("COST_CATEGORY:"):
        return f"Cost category: {dim[14:]}"
    return dim


def next_dimension(group_by: str) -> str:
    """Where drilling into a row of each breakdown leads next (same as the web UI)."""
    if group_by == "REGION":
        return "LINKED_ACCOUNT"
    if group_by in ("LINKED_ACCOUNT", "DIRECTION"):
        return "TRANSFER_TYPE"
    if group_by == "TRANSFER_TYPE":
        return "USAGE_TYPE"
    if group_by == "USAGE_TYPE" or group_by.startswith("COST_CATEGORY:"):
        return "LINKED_ACCOUNT"
    return "TRANSFER_TYPE"  # SERVICE, TAG:*


class DtoService:
    def __init__(self, ce: CostExplorer, cache: Cache, source: str = "aws", dimensions: set[str] | None = None):
        self.ce = ce
        self.cache = cache
        self.source = source
        self.mock = source == "mock"
        # OPTIONAL_DIMENSIONS the data has (None: all of them).
        self.dimensions = dimensions

    def dto_usage_types(self, start: date, end: date) -> dict[str, str]:
        """All outbound-transfer usage types with data in the window, mapped to transfer type."""

        def load() -> dict[str, str]:
            found: dict[str, str] = {}
            for s in _SEARCH_STRINGS:
                for v in self.ce.dimension_values("USAGE_TYPE", start, end, search=s):
                    kind = classify(v["Value"])
                    if kind:
                        found[v["Value"]] = kind
            return found

        return self.cache.get_or_set(("usage_types", start, end), load)

    def _account_names(self) -> dict[str, str]:
        return {o.value: o.label for o in self.meta().accounts}

    def summary(
        self,
        period: PeriodKind,
        start: date | None,
        end: date | None,
        group_by: str,
        filters: dict[str, list[str]],
        include_nat_volume: bool = False,
        include_aws_internal: bool = False,
        today: date | None = None,
    ) -> Summary:
        _validate_dimension(group_by)
        try:
            resolved = resolve(period, start, end, today=today)
        except ValueError as e:
            raise DtoQueryError(str(e)) from e

        key = ("summary", period, resolved.display, group_by, tuple(sorted((k, tuple(sorted(v))) for k, v in filters.items())), include_nat_volume, include_aws_internal, resolved.query.end)
        return self.cache.get_or_set(key, lambda: self._summary(period, resolved, group_by, filters, include_nat_volume, include_aws_internal))

    def _summary(
        self, period: str, resolved: ResolvedPeriod, group_by: str, filters: dict[str, list[str]], include_nat_volume: bool, include_aws_internal: bool
    ) -> Summary:
        q = resolved.query
        result = Summary(
            period=period,
            window=resolved.display,
            data_window=None if q.is_empty else q,
            previous_window=None if q.is_empty else resolved.previous,
            group_by=group_by,
            include_nat_volume=include_nat_volume,
            include_aws_internal=include_aws_internal,
            by_type={t: 0.0 for t in TRANSFER_TYPES},
            mock=self.mock,
            source=self.source,
        )
        if q.is_empty:
            return result

        usage_types = self._filtered_usage_types(q.start, q.end, filters, include_aws_internal)
        if not usage_types:
            return result

        ce_group = [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}]
        if group_by in ("REGION", "LINKED_ACCOUNT", "SERVICE"):
            ce_group.append({"Type": "DIMENSION", "Key": group_by})
        elif group_by.startswith("TAG:"):
            ce_group.append({"Type": "TAG", "Key": group_by[4:]})
        elif group_by.startswith("COST_CATEGORY:"):
            ce_group.append({"Type": "COST_CATEGORY", "Key": group_by[14:]})

        results, attrs = self.ce.cost_and_usage(q.start, q.end, q.granularity, ["UsageQuantity"], self._filter_expr(usage_types, filters), ce_group)

        rows: dict[str, Row] = {}
        series: dict[str, dict[str, float]] = {}
        volume: dict[str, dict[str, float]] = {}
        for r in results:
            result.estimated |= bool(r.get("Estimated"))
            bucket = series.setdefault(r["TimePeriod"]["Start"], defaultdict(float))
            vbucket = volume.setdefault(r["TimePeriod"]["Start"], defaultdict(float))
            for g in r.get("Groups", []):
                ut = g["Keys"][0]
                kind = usage_types.get(ut)
                if kind is None:
                    continue
                gb = counted_gb(kind, float(g["Metrics"]["UsageQuantity"]["Amount"]), include_nat_volume)
                if not gb:
                    continue

                if group_by == "TRANSFER_TYPE":
                    gkey = kind
                elif group_by == "DIRECTION":
                    gkey = DIRECTION[kind]
                elif group_by == "USAGE_TYPE":
                    gkey = ut
                else:
                    gkey = _split_group_value(g["Keys"][1])

                row = rows.get(gkey)
                if row is None:
                    row = rows[gkey] = Row(key=gkey, label=self._label(group_by, gkey, attrs), gb=0, pct=0, by_type={})
                row.gb += gb
                row.by_type[kind] = row.by_type.get(kind, 0.0) + gb
                bucket[gkey] += gb
                vbucket[kind] += gb
                result.by_type[kind] += gb
                result.total_gb += gb

        result.egress_gb, result.ingress_gb, result.transit_gb = _by_direction(result.by_type)
        for row in rows.values():
            row.pct = row.gb / result.total_gb * 100 if result.total_gb else 0.0
        result.rows = sorted(rows.values(), key=lambda x: x.gb, reverse=True)
        result.series = [SeriesPoint(date=date.fromisoformat(d), values=dict(v)) for d, v in sorted(series.items())]
        result.volume_series = [SeriesPoint(date=date.fromisoformat(d), values=dict(v)) for d, v in sorted(volume.items())]

        prev = self._previous_by_type(resolved.previous, filters, include_nat_volume, include_aws_internal)
        result.prev_total_gb = sum(prev.values())
        result.prev_egress_gb, result.prev_ingress_gb, result.prev_transit_gb = _by_direction(prev)
        return result

    def _previous_by_type(self, w: Window, filters: dict[str, list[str]], include_nat_volume: bool, include_aws_internal: bool) -> dict[str, float]:
        """Counted GB per transfer type for the window, in one MONTHLY call grouped by usage type."""
        usage_types = self._filtered_usage_types(w.start, w.end, filters, include_aws_internal)
        out: dict[str, float] = defaultdict(float)
        if not usage_types:
            return out
        results, _ = self.ce.cost_and_usage(
            w.start, w.end, "MONTHLY", ["UsageQuantity"], self._filter_expr(usage_types, filters), [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}]
        )
        for r in results:
            for g in r.get("Groups", []):
                kind = usage_types.get(g["Keys"][0])
                if kind is not None:
                    out[kind] += counted_gb(kind, float(g["Metrics"]["UsageQuantity"]["Amount"]), include_nat_volume)
        return out

    def _filtered_usage_types(self, start: date, end: date, filters: dict[str, list[str]], include_aws_internal: bool) -> dict[str, str]:
        usage_types = self.dto_usage_types(start, end)
        if not include_aws_internal:
            usage_types = {u: k for u, k in usage_types.items() if k not in AWS_INTERNAL_TYPES}
        if types := filters.get("TRANSFER_TYPE"):
            usage_types = {u: k for u, k in usage_types.items() if k in types}
        if directions := filters.get("DIRECTION"):
            usage_types = {u: k for u, k in usage_types.items() if DIRECTION[k] in directions}
        if uts := filters.get("USAGE_TYPE"):
            usage_types = {u: k for u, k in usage_types.items() if u in uts}
        return usage_types

    @staticmethod
    def _filter_expr(usage_types: dict[str, str], filters: dict[str, list[str]]) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [
            {"Dimensions": {"Key": "USAGE_TYPE", "Values": sorted(usage_types)}},
            # Credits, refunds and discounts are booked on the same usage types with no volume; they would only add empty groups.
            {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
        ]
        for dim, values in sorted(filters.items()):
            if dim in ("TRANSFER_TYPE", "DIRECTION", "USAGE_TYPE"):
                continue  # applied by narrowing the usage type list
            if dim.startswith("TAG:"):
                parts.append(_keyed_filter("Tags", dim[4:], values))
            elif dim.startswith("COST_CATEGORY:"):
                parts.append(_keyed_filter("CostCategories", dim[14:], values))
            else:
                parts.append({"Dimensions": {"Key": dim, "Values": values}})
        return _and(parts)

    def _label(self, group_by: str, key: str, attrs: dict[str, str]) -> str:
        if key == "":
            return UNTAGGED if group_by.startswith(("TAG:", "COST_CATEGORY:")) else NO_VALUE
        if group_by == "REGION":
            return REGION_NAMES.get(key, key)
        if group_by == "LINKED_ACCOUNT":
            name = attrs.get(key) or self._account_names().get(key)
            return name if name and name != key else key
        return key

    def meta(self, today: date | None = None) -> Meta:
        today = today or date.today()
        end = today + timedelta(days=1)
        start = (today - timedelta(days=90)).replace(day=1)

        def load() -> Meta:
            accounts = [
                Option(value=v["Value"], label=v.get("Attributes", {}).get("description") or v["Value"])
                for v in self.ce.dimension_values("LINKED_ACCOUNT", start, end)
            ]
            regions = [
                Option(value=v["Value"], label=REGION_NAMES.get(v["Value"], v["Value"]))
                for v in self.ce.dimension_values("REGION", start, end)
                if v["Value"]  # Cost Explorer reports some usage under an empty region
            ]
            services = sorted(v["Value"] for v in self.ce.dimension_values("SERVICE", start, end))
            try:
                tag_keys = self.ce.tag_keys(start, end)
            except Exception:
                tag_keys = []
            try:
                cost_categories = self.ce.cost_category_names(start, end)
            except Exception:
                cost_categories = []

            group_opts = [
                Option(value=k, label=v)
                for k, v in DIMENSIONS.items()
                if self.dimensions is None or k not in OPTIONAL_DIMENSIONS or k in self.dimensions
            ]
            group_opts += [Option(value=f"TAG:{k}", label=f"Tag: {k}") for k in tag_keys]
            group_opts += [Option(value=f"COST_CATEGORY:{c}", label=f"Cost category: {c}") for c in cost_categories]
            return Meta(
                accounts=sorted(accounts, key=lambda o: o.label.lower()),
                regions=sorted(regions, key=lambda o: o.value),
                services=services,
                tag_keys=tag_keys,
                cost_categories=cost_categories,
                transfer_types=TRANSFER_TYPES,
                directions=DIRECTION,
                aws_internal_types=AWS_INTERNAL_TYPES,
                group_by_options=group_opts,
                mock=self.mock,
                source=self.source,
            )

        return self.cache.get_or_set(("meta", start, end), load)


# =====================================================================================
# Export and formatting
# =====================================================================================


def csv_filename(s: Summary) -> str:
    return f"dto_{s.window.start}_{s.window.last_day}_{s.group_by.replace(':', '-')}.csv"


def write_csv(s: Summary, path: Path) -> None:
    """Same columns as the web app's /api/dto/export.csv."""
    types = list(s.by_type)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([s.group_by, "label", "gb", "pct", *[f"gb_{t}" for t in types]])
        for r in s.rows:
            writer.writerow([r.key, r.label, f"{r.gb:.4f}", f"{r.pct:.2f}", *[f"{r.by_type.get(t, 0):.4f}" for t in types]])


def fmt_gb(v: float) -> str:
    for unit, scale in (("PB", 1e6), ("TB", 1e3)):
        if abs(v) >= scale:
            return f"{v / scale:,.2f} {unit}"
    return f"{v:,.1f} GB"


def fmt_delta(cur: float, prev: float | None) -> str:
    if prev is None:
        return ""
    if prev == 0:
        return "[dim]no previous data[/]"
    pct = (cur - prev) / abs(prev) * 100
    color = "red" if pct > 0 else "green"  # more traffic usually means more spend, so going up is flagged
    arrow = "▲" if pct > 0 else "▼"
    return f"[{color}]{arrow} {abs(pct):.1f}%[/] [dim]vs previous[/]"


def bar(fraction: float, width: int = 20) -> str:
    fraction = min(max(fraction, 0.0), 1.0)
    full, rest = divmod(round(fraction * width * 8), 8)
    return ("█" * full + (" ▏▎▍▌▋▊▉"[rest] if rest else "")).ljust(width)


# =====================================================================================
# Textual UI
# =====================================================================================

from textual import on, work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import (  # noqa: E402
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    SelectionList,
    Sparkline,
    Static,
    Switch,
)
from textual.widgets.selection_list import Selection  # noqa: E402

# Dimensions offered in the filter dialog (others are reachable through drill-down).
FILTER_DIMS = ("LINKED_ACCOUNT", "REGION", "DIRECTION", "TRANSFER_TYPE")
SOURCE_LABELS = {"aws": "AWS Cost Explorer", "mock": "mock data", "csv": "CSV data"}


def available_dims(meta: Meta | None) -> set[str] | None:
    """Breakdowns the data has (CSV files may lack region / account / service), or None for all."""
    return None if meta is None else {o.value for o in meta.group_by_options}


def fallback_dim(dim: str, meta: Meta | None) -> str:
    """A breakdown on a column the CSV data lacks falls back to transfer type (as in the web UI)."""
    available = available_dims(meta)
    return "TRANSFER_TYPE" if available is not None and dim in OPTIONAL_DIMENSIONS and dim not in available else dim


class FilterScreen(ModalScreen[dict[str, list[str]] | None]):
    """Multi-select account / region / direction / transfer type filters."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, meta: Meta, filters: dict[str, list[str]], include_aws_internal: bool):
        super().__init__()
        self.meta = meta
        self.filters = filters
        self.include_aws_internal = include_aws_internal
        available = available_dims(meta) or set()
        self.dims = [d for d in FILTER_DIMS if d not in OPTIONAL_DIMENSIONS or d in available]

    def compose(self) -> ComposeResult:
        types = [t for t in self.meta.transfer_types if self.include_aws_internal or t not in self.meta.aws_internal_types]
        options = {
            "LINKED_ACCOUNT": [(f"{o.label} ({o.value})" if o.label != o.value else o.value, o.value) for o in self.meta.accounts],
            "REGION": [(f"{o.value}  {o.label}" if o.label != o.value else o.value, o.value) for o in self.meta.regions],
            "DIRECTION": [(d, d) for d in DIRECTIONS],
            "TRANSFER_TYPE": [(t, t) for t in types],
        }
        with Vertical(id="filter-dialog"):
            yield Label("[b]Filters[/]  [dim]space toggles · values are OR'd, dimensions AND'd[/]")
            with Horizontal(id="filter-lists"):
                for dim in self.dims:
                    current = set(self.filters.get(dim, []))
                    with Vertical(classes="filter-col"):
                        yield Label(DIMENSIONS[dim])
                        yield SelectionList[str](*[Selection(p, v, v in current) for p, v in options[dim]], id=f"f-{dim}")
            with Horizontal(id="filter-buttons"):
                yield Button("Apply", variant="primary", id="apply")
                yield Button("Clear these", id="clear")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#apply")
    def _apply(self) -> None:
        out = {k: v for k, v in self.filters.items() if k not in self.dims}
        for dim in self.dims:
            if selected := self.query_one(f"#f-{dim}", SelectionList).selected:
                out[dim] = list(selected)
        self.dismiss(out)

    @on(Button.Pressed, "#clear")
    def _clear(self) -> None:
        for dim in self.dims:
            self.query_one(f"#f-{dim}", SelectionList).deselect_all()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass
class ViewState:
    period: PeriodKind = "month"
    anchor: date = field(default_factory=date.today)  # any day in the week / month
    custom_start: date | None = None
    custom_end: date | None = None
    group_by: str = "REGION"
    include_nat_volume: bool = False
    include_aws_internal: bool = False
    filters: dict[str, list[str]] = field(default_factory=dict)


class DtoApp(App):
    TITLE = "DTO Explorer"
    CSS = """
    #toolbar, #controls { height: auto; padding: 0 1; }
    #toolbar > *, #controls > * { margin-right: 1; }
    #period { width: 16; }
    #preset { width: 22; }
    #group_by { width: 34; }
    #window-label { width: auto; padding: 1 1 0 0; }
    .date { width: 16; }
    .switch-label { padding: 1 0 0 1; width: auto; }
    #filters-line { height: auto; padding: 0 2; color: $text-muted; }
    #kpis { height: 5; padding: 0 1; }
    .kpi { width: 1fr; height: 5; border: round $primary 50%; padding: 0 1; }
    #charts { height: 10; padding: 0 1; }
    #spark-box { width: 1fr; border: round $primary 50%; padding: 0 1; }
    #spark-box Sparkline { height: 2; margin-bottom: 1; }
    #types { width: 1fr; border: round $primary 50%; padding: 0 1; }
    #search { margin: 0 1; }
    #table { height: 1fr; margin: 0 1; }
    FilterScreen { align: center middle; }
    #filter-dialog { width: 90%; height: 80%; border: thick $primary; background: $surface; padding: 1 2; }
    #filter-lists { height: 1fr; }
    .filter-col { width: 1fr; padding-right: 1; }
    .filter-col SelectionList { height: 1fr; }
    #filter-buttons { height: auto; align-horizontal: right; }
    #filter-buttons Button { margin-left: 1; }
    """
    BINDINGS = [
        Binding("left_square_bracket", "shift(-1)", "Prev"),
        Binding("right_square_bracket", "shift(1)", "Next"),
        Binding("f", "filters", "Filters"),
        Binding("backspace", "drill_up", "Back"),
        Binding("x", "clear_filters", "Clear filters"),
        Binding("slash", "search", "Search"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("e", "export", "Export CSV"),
        Binding("r", "refresh", "Refresh"),
        Binding("d", "toggle_theme", "Theme"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, service: DtoService, state: ViewState, tag_key: str | None = None):
        super().__init__()
        self.service = service
        self.state = state
        self.tag_key = tag_key
        self.meta: Meta | None = None
        self.summary: Summary | None = None
        self.drill_stack: list[tuple[str, dict[str, list[str]]]] = []  # (group_by, filters) before each drill
        self.sort_col = 2  # index into SORT_KEYS (GB)
        self.sort_reverse = True
        self.search = ""
        self.visible_rows: list[Row] = []

    # ---- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="toolbar"):
            yield Select([("Week", "week"), ("Month", "month"), ("Custom", "custom")], value=self.state.period, allow_blank=False, id="period")
            yield Static("", id="window-label")
            yield Input(placeholder="start YYYY-MM-DD", id="start", classes="date")
            yield Input(placeholder="end YYYY-MM-DD", id="end", classes="date")
            yield Select([(k, k) for k in presets()], prompt="Presets", id="preset")
        with Horizontal(id="controls"):
            yield Select([(v, k) for k, v in DIMENSIONS.items()], value="REGION", allow_blank=False, id="group_by")
            yield Label("Include traffic within AWS", classes="switch-label")
            yield Switch(value=self.state.include_aws_internal, id="aws-internal")
            yield Label("Include NAT volume", classes="switch-label")
            yield Switch(value=self.state.include_nat_volume, id="nat")
        yield Static("", id="filters-line")
        with Horizontal(id="kpis"):
            for k in ("kpi-egress", "kpi-ingress", "kpi-transit", "kpi-top"):
                yield Static("", id=k, classes="kpi")
        with Horizontal(id="charts"):
            with VerticalScroll(id="spark-box"):
                yield Static("Egress over time", id="spark-out-label")
                yield Sparkline([], id="spark-out")
                yield Static("Ingress over time", id="spark-in-label")
                yield Sparkline([], id="spark-in")
            yield Static("", id="types")
        yield Input(placeholder="/ search rows…", id="search")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = SOURCE_LABELS[self.service.source]
        self._sync_period_widgets()
        self.query_one("#table", DataTable).focus()
        if info := self._csv_info():
            self.notify(info, title="CSV data", timeout=8)
        self.load_meta()

    def _csv_info(self) -> str:
        """Files, rows and dates read with --csv (what DTO Explorer reports in /api/health)."""
        client = self.service.ce.client
        if not isinstance(client, CsvCostExplorerClient):
            return ""
        data = client.data
        dates = f"{data.date_range[0]} → {data.date_range[1]}" if data.date_range else "no rows"
        files = str(data.files[0]) if len(data.files) == 1 else f"{len(data.files)} files in {client.path}"
        return f"{data.rows:,} rows from {files}, {dates}"

    # ---- data loading ----------------------------------------------------

    @work(thread=True, exclusive=True, group="meta")
    def load_meta(self) -> None:
        try:
            meta = self.service.meta()
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._show_error, e)
            meta = None
        self.call_from_thread(self._meta_loaded, meta)

    def _meta_loaded(self, meta: Meta | None) -> None:
        self.meta = meta
        if meta:
            if self.state.group_by == "REGION" and self.tag_key and self.tag_key in meta.tag_keys:
                self.state.group_by = f"TAG:{self.tag_key}"
            self.state.group_by = fallback_dim(self.state.group_by, meta)
            opts = [(o.label, o.value) for o in meta.group_by_options]
            if self.state.group_by not in {o.value for o in meta.group_by_options}:
                opts.append((dim_label(self.state.group_by), self.state.group_by))
            with self._sync():
                sel = self.query_one("#group_by", Select)
                sel.set_options(opts)
                sel.value = self.state.group_by
            if not meta.tag_keys and meta.source == "csv":
                self.notify("No tag:<key> columns in the CSV files. Export with a tag (e.g. TAG:vpc) to break data volume down by VPC or team.", timeout=8)
            elif not meta.tag_keys:
                self.notify("No cost allocation tags found. Activate tags (e.g. vpc) in Billing to break data volume down by VPC or team.", timeout=8)
        self.reload()

    def reload(self) -> None:
        self._sync_period_widgets()
        self._render_filters()
        self.query_one("#table", DataTable).loading = True
        self.load_summary(replace(self.state, filters={k: list(v) for k, v in self.state.filters.items()}))

    @work(thread=True, exclusive=True, group="summary")
    def load_summary(self, st: ViewState) -> None:
        if st.period == "custom":
            start, end = st.custom_start, st.custom_end
        else:
            start, end = st.anchor, None
        try:
            s = self.service.summary(st.period, start, end, st.group_by, st.filters, st.include_nat_volume, st.include_aws_internal)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._show_error, e)
            s = None
        self.call_from_thread(self._summary_loaded, s)

    def _show_error(self, e: Exception) -> None:
        msg = str(e)
        try:
            from botocore.exceptions import ClientError, NoCredentialsError, TokenRetrievalError

            if isinstance(e, ClientError):
                err = e.response.get("Error", {})
                msg = f"AWS {err.get('Code', 'error')}: {err.get('Message', msg)}"
            if isinstance(e, (NoCredentialsError, TokenRetrievalError)) or "expired" in msg.lower() or "sso" in msg.lower():
                msg += "\nTry `aws sso login --profile <profile>`, then press r."
        except ImportError:
            pass
        self.notify(msg, title="Error", severity="error", timeout=15)

    def _summary_loaded(self, s: Summary | None) -> None:
        self.query_one("#table", DataTable).loading = False
        if s is None:
            return
        self.summary = s
        self._render_summary()

    # ---- rendering -------------------------------------------------------

    def _sync_period_widgets(self) -> None:
        st = self.state
        custom = st.period == "custom"
        with self._sync():
            self.query_one("#period", Select).value = st.period
            for wid, value in (("#start", st.custom_start), ("#end", st.custom_end)):
                inp = self.query_one(wid, Input)
                inp.display = custom
                if value and inp.value != value.isoformat():
                    inp.value = value.isoformat()
            self.query_one("#preset", Select).display = custom
        if custom:
            label = "Custom range:"
        else:
            r = resolve(st.period, st.anchor)
            w = r.display
            label = f"{w.start:%a %d %b} – {w.last_day:%a %d %b %Y}" if st.period == "week" else f"{w.start:%B %Y}"
            label += "  [dim]\\[ ] prev/next[/]"
        self.query_one("#window-label", Static).update(label)

    def _render_filters(self) -> None:
        parts = []
        for dim, values in sorted(self.state.filters.items()):
            shown = [self._value_label(dim, v) for v in values]
            parts.append(f"[b]{dim_label(dim)}[/] = {' | '.join(shown)}")
        drill = f"  [dim](drill depth {len(self.drill_stack)}, backspace to go back)[/]" if self.drill_stack else ""
        text = ("Filters: " + "  ·  ".join(parts) + drill + "  [dim]x clears[/]") if parts else "[dim]No filters · f to add · Enter on a row drills down[/]"
        self.query_one("#filters-line", Static).update(text)

    def _value_label(self, dim: str, value: str) -> str:
        if dim == "LINKED_ACCOUNT" and self.meta:
            for o in self.meta.accounts:
                if o.value == value and o.label != value:
                    return f"{o.label} ({value})"
        if dim.startswith(("TAG:", "COST_CATEGORY:")) and value == "":
            return UNTAGGED
        return value

    def _render_summary(self) -> None:
        s = self.summary
        assert s is not None
        badges = " · ".join(b for b in (SOURCE_LABELS[s.source], "estimated" if s.estimated else "") if b)
        self.sub_title = badges
        dw = s.data_window
        prev = s.previous_window
        win = f"{s.window.start} → {s.window.last_day}"
        if dw and dw.end != s.window.end:
            win += f" (data to {dw.last_day})"
        if self.state.period == "custom":
            self.query_one("#window-label", Static).update(f"Custom: {win}")

        # Same tiles as the web UI: egress and ingress, transit only when something can be in it.
        internet_only = not s.include_aws_internal
        prev_txt = f"[dim]vs {prev.start} → {prev.last_day}[/]" if prev else "[dim]no data in range[/]"

        def tile(title: str, gb: float, prev_gb: float | None) -> str:
            delta = fmt_delta(gb, prev_gb) if prev_gb is not None else prev_txt
            return f"[dim]{title}[/]\n[b]{fmt_gb(gb)}[/]\n{delta}"

        self.query_one("#kpi-egress", Static).update(tile("Internet egress" if internet_only else "Egress", s.egress_gb, s.prev_egress_gb))
        self.query_one("#kpi-ingress", Static).update(tile("Internet ingress" if internet_only else "Ingress", s.ingress_gb, s.prev_ingress_gb))
        transit = self.query_one("#kpi-transit", Static)
        transit.display = s.include_aws_internal or s.include_nat_volume
        transit.update(tile("Transit inside AWS", s.transit_gb, s.prev_transit_gb))
        top = s.rows[0] if s.rows else None
        top_txt = f"[b]{top.label}[/]\n{fmt_gb(top.gb)} · {top.pct:.1f}% of total" if top else "–"
        self.query_one("#kpi-top", Static).update(f"[dim]Top {dim_label(s.group_by).lower()}[/]\n{top_txt}")

        by_dir = [_by_direction(p.values) for p in s.volume_series]
        outs = [d[0] for d in by_dir]
        ins = [d[1] for d in by_dir]
        step = "daily" if s.window.granularity == "DAILY" else "monthly"
        rng = f"{s.volume_series[0].date} → {s.volume_series[-1].date}" if s.volume_series else "no data"
        self.query_one("#spark-out-label", Static).update(f"Egress, {step} [dim]{rng}  peak {fmt_gb(max(outs, default=0))}[/]")
        self.query_one("#spark-out", Sparkline).data = outs or [0]
        self.query_one("#spark-in-label", Static).update(f"Ingress, {step} [dim]peak {fmt_gb(max(ins, default=0))}[/]")
        self.query_one("#spark-in", Sparkline).data = ins or [0]

        lines = [f"[b]By transfer type[/] · {fmt_gb(s.total_gb)}   [dim]volume share[/]"]
        for t, gb in s.by_type.items():
            if not gb:
                continue
            frac = gb / s.total_gb if s.total_gb else 0
            name = f"{t} (transit)" if DIRECTION[t] == TRANSIT else t
            lines.append(f"{name:<24} [$accent]{bar(frac, 16)}[/] {frac * 100:5.1f}%  {fmt_gb(gb):>11}")
        if len(lines) == 1:
            lines.append("[dim]No data transfer in this window.[/]")
        self.query_one("#types", Static).update("\n".join(lines))

        self._render_table()

    SORT_KEYS: list[tuple[str, Callable[[Row], Any]]] = [
        ("label", lambda r: r.label.lower()),
        ("key", lambda r: r.key.lower()),
        ("gb", lambda r: r.gb),
        ("pct", lambda r: r.pct),
    ]

    def _render_table(self) -> None:
        s = self.summary
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        if s is None:
            return
        types = [t for t, gb in s.by_type.items() if gb]
        arrow = " ▼" if self.sort_reverse else " ▲"
        heads = [dim_label(s.group_by), "Key", "GB", "%"]
        heads[self.sort_col] += arrow
        table.add_columns(*heads, *[f"{t} GB" for t in types])

        q = self.search.lower()
        rows = [r for r in s.rows if not q or q in r.label.lower() or q in r.key.lower()]
        rows.sort(key=self.SORT_KEYS[self.sort_col][1], reverse=self.sort_reverse)
        self.visible_rows = rows
        for i, r in enumerate(rows):
            table.add_row(
                r.label,
                r.key if r.key != r.label else "",
                f"{r.gb:,.1f}",
                f"{r.pct:.1f}",
                *[f"{r.by_type.get(t, 0.0):,.1f}" for t in types],
                key=str(i),
            )
        if not rows:
            table.add_row("[dim]no rows[/]", "", "", "", *["" for _ in types])

    # ---- events ----------------------------------------------------------

    def _sync(self) -> Any:
        """Set widgets programmatically without triggering their Changed handlers."""
        return self.prevent(Select.Changed, Switch.Changed, Input.Changed)

    @on(Select.Changed, "#period")
    def _period_changed(self, event: Select.Changed) -> None:
        if event.value == self.state.period or event.value is Select.BLANK:
            return
        if event.value == "custom" and not (self.state.custom_start and self.state.custom_end):
            r = resolve(self.state.period, self.state.anchor)
            self.state.custom_start, self.state.custom_end = r.display.start, min(r.display.last_day, date.today())
        self.state.period = event.value  # type: ignore[assignment]
        self.reload()

    @on(Select.Changed, "#preset")
    def _preset_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        self.state.custom_start, self.state.custom_end = presets()[str(event.value)]
        self.state.period = "custom"
        with self._sync():
            event.select.clear()
        self.reload()

    @on(Input.Submitted, "#start, #end")
    def _dates_submitted(self) -> None:
        try:
            start = date.fromisoformat(self.query_one("#start", Input).value.strip())
            end = date.fromisoformat(self.query_one("#end", Input).value.strip())
        except ValueError:
            self.notify("Dates must look like YYYY-MM-DD", severity="warning")
            return
        if end < start:
            self.notify("End must be on or after start", severity="warning")
            return
        self.state.custom_start, self.state.custom_end = start, end
        self.reload()
        self.query_one("#table", DataTable).focus()

    @on(Select.Changed, "#group_by")
    def _group_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK or event.value == self.state.group_by:
            return
        self.state.group_by = str(event.value)
        self.drill_stack.clear()
        self.reload()

    @on(Switch.Changed, "#aws-internal")
    def _aws_internal_changed(self, event: Switch.Changed) -> None:
        if event.value == self.state.include_aws_internal:
            return
        self.state.include_aws_internal = event.value
        # Hiding traffic within AWS also drops any transfer type filter on those types, which would otherwise match nothing.
        if not event.value and "TRANSFER_TYPE" in self.state.filters:
            types = [t for t in self.state.filters["TRANSFER_TYPE"] if t not in AWS_INTERNAL_TYPES]
            if types:
                self.state.filters["TRANSFER_TYPE"] = types
            else:
                del self.state.filters["TRANSFER_TYPE"]
        self.reload()

    @on(Switch.Changed, "#nat")
    def _nat_changed(self, event: Switch.Changed) -> None:
        if event.value == self.state.include_nat_volume:
            return
        self.state.include_nat_volume = event.value
        self.reload()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.search = event.value.strip()
        self._render_table()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.query_one("#table", DataTable).focus()

    @on(DataTable.HeaderSelected)
    def _header_selected(self, event: DataTable.HeaderSelected) -> None:
        if event.column_index >= len(self.SORT_KEYS):
            return
        if event.column_index == self.sort_col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col, self.sort_reverse = event.column_index, event.column_index >= 2  # numbers sort descending
        self._render_table()

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        """Drill down: filter on the selected row and break down by the next dimension."""
        if self.summary is None or event.row_key.value is None:
            return
        idx = int(event.row_key.value)
        if idx >= len(self.visible_rows):
            return
        row = self.visible_rows[idx]
        gb = self.state.group_by
        self.drill_stack.append((gb, {k: list(v) for k, v in self.state.filters.items()}))
        current = self.state.filters.get(gb, [])
        if row.key not in current:
            self.state.filters[gb] = [*current, row.key]
        self.state.group_by = fallback_dim(next_dimension(gb), self.meta)
        self._set_group_select()
        self.search = ""
        self.query_one("#search", Input).value = ""
        self.reload()

    def _set_group_select(self) -> None:
        sel = self.query_one("#group_by", Select)
        with self._sync():
            sel.value = self.state.group_by

    # ---- actions ---------------------------------------------------------

    def action_shift(self, step: int) -> None:
        st = self.state
        today = date.today()
        if st.period == "week":
            new = st.anchor + timedelta(days=7 * step)
            if resolve("week", new).display.start > today:
                return
            st.anchor = new
        elif st.period == "month":
            new = _add_months(st.anchor.replace(day=1), step)
            if new > today:
                return
            st.anchor = new
        else:
            if not (st.custom_start and st.custom_end):
                return
            length = st.custom_end - st.custom_start + timedelta(days=1)
            if step > 0 and st.custom_start + length > today:
                return
            st.custom_start += length * step
            st.custom_end += length * step
        self.reload()

    def action_filters(self) -> None:
        if self.meta is None:
            self.notify("Metadata is still loading", severity="warning")
            return

        def done(result: dict[str, list[str]] | None) -> None:
            if result is not None and result != self.state.filters:
                self.state.filters = result
                self.drill_stack.clear()
                self.reload()

        self.push_screen(FilterScreen(self.meta, self.state.filters, self.state.include_aws_internal), done)

    def action_drill_up(self) -> None:
        if not self.drill_stack:
            return
        self.state.group_by, self.state.filters = self.drill_stack.pop()
        self._set_group_select()
        self.reload()

    def action_clear_filters(self) -> None:
        if self.state.filters:
            self.state.filters = {}
            self.drill_stack.clear()
            self.reload()

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_cycle_sort(self) -> None:
        self.sort_col = (self.sort_col + 1) % len(self.SORT_KEYS)
        self.sort_reverse = self.sort_col >= 2
        self._render_table()

    def action_export(self) -> None:
        if self.summary is None:
            self.notify("Nothing to export yet", severity="warning")
            return
        path = Path.cwd() / csv_filename(self.summary)
        try:
            write_csv(self.summary, path)
        except OSError as e:
            self.notify(f"Export failed: {e}", severity="error")
            return
        self.notify(f"Exported {len(self.summary.rows)} rows to {path}")

    def action_refresh(self) -> None:
        if isinstance(self.service.ce.client, CsvCostExplorerClient):
            self.reload_csv()
            return
        self.service.cache.clear()
        self.notify("Cache cleared, fetching fresh data (each Cost Explorer request costs $0.01)")
        self.load_meta()

    @work(thread=True, exclusive=True, group="csv")
    def reload_csv(self) -> None:
        """Re-read the CSV files; if they are invalid, keep the data already loaded."""
        try:
            self.service.ce.client.reload()
        except CsvDataError as e:
            self.call_from_thread(self.notify, f"{e}\nKeeping the data already loaded.", title="CSV data", severity="error", timeout=15)
            return
        self.service.cache.clear()
        self.call_from_thread(self.notify, f"Re-read the CSV files: {self._csv_info()}", title="CSV data")
        self.call_from_thread(self.load_meta)

    def action_toggle_theme(self) -> None:
        self.theme = "textual-light" if self.current_theme.dark else "textual-dark"

    def action_help(self) -> None:
        self.notify(
            "[ / ] previous/next period · f filters · Enter drill down · Backspace back · x clear filters\n"
            "/ search · s or click header to sort · e export CSV · r refresh (re-reads --csv files) · d theme · q quit\n"
            "Tab moves between the period, group-by, traffic within AWS and NAT volume controls.",
            title="Keys",
            timeout=12,
        )


# =====================================================================================
# Entry point
# =====================================================================================


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Explore AWS data transfer volume in the terminal.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--mock", action="store_true", default=_env_bool("DTO_MOCK"), help="use synthetic data instead of AWS (DTO_MOCK)")
    p.add_argument(
        "--csv",
        type=Path,
        default=os.environ.get("DTO_CSV_PATH") or None,
        metavar="PATH",
        help="read usage from a CSV file, or every *.csv file in a folder, instead of AWS; see docs/csv-data.md (DTO_CSV_PATH)",
    )
    p.add_argument("--profile", default=os.environ.get("DTO_AWS_PROFILE"), help="AWS profile (DTO_AWS_PROFILE, else the standard boto3 chain incl. AWS_PROFILE)")
    p.add_argument("--ce-region", default=os.environ.get("DTO_CE_REGION", "us-east-1"), help="Cost Explorer endpoint region (DTO_CE_REGION)")
    p.add_argument("--cache-ttl", type=int, default=int(os.environ.get("DTO_CACHE_TTL_SECONDS", "3600")), help="cache lifetime in seconds, 0 disables (DTO_CACHE_TTL_SECONDS)")
    p.add_argument("--tag-key", default=os.environ.get("DTO_DEFAULT_TAG_KEY"), help="tag key to group by initially, e.g. vpc (DTO_DEFAULT_TAG_KEY)")
    p.add_argument("--period", choices=["week", "month", "custom"], default="month")
    p.add_argument("--start", type=date.fromisoformat, help="YYYY-MM-DD; a day in the week/month, or the custom range start")
    p.add_argument("--end", type=date.fromisoformat, help="YYYY-MM-DD; custom range end (inclusive)")
    p.add_argument("--group-by", help="REGION, LINKED_ACCOUNT, SERVICE, TRANSFER_TYPE, DIRECTION, USAGE_TYPE, TAG:<key> or COST_CATEGORY:<name>")
    p.add_argument("--include-aws-internal", action="store_true", help="also count Inter-AZ, Inter-Region Out/In and To CloudFront, not just internet traffic")
    p.add_argument("--include-nat-volume", action="store_true", help="add NAT Gateway data processed to transit (it duplicates the traffic it carries)")
    p.add_argument("--filter", action="append", default=[], metavar="DIM=VALUE", help="repeatable, e.g. REGION=us-east-1 or TAG:vpc=prod")
    args = p.parse_args(argv)
    if args.mock and args.csv:
        p.error("--csv can't be combined with --mock (or DTO_MOCK)")
    if args.period == "custom" and not (args.start and args.end):
        p.error("--period custom needs --start and --end")
    try:
        if args.group_by:
            _validate_dimension(args.group_by)
        args.filters = parse_filters(args.filter)
    except DtoQueryError as e:
        p.error(str(e))
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        ce = make_cost_explorer(args.mock, args.profile, args.ce_region, args.csv)
    except CsvDataError as e:
        print(f"dto_tui.py: CSV data: {e}", file=sys.stderr)
        sys.exit(2)
    source = "mock" if args.mock else "csv" if args.csv else "aws"
    service = DtoService(ce, Cache(args.cache_ttl), source=source, dimensions=getattr(ce.client, "dimensions", None))
    state = ViewState(
        period=args.period,
        anchor=args.start or date.today(),
        custom_start=args.start if args.period == "custom" else None,
        custom_end=args.end if args.period == "custom" else None,
        group_by=args.group_by or "REGION",
        include_nat_volume=args.include_nat_volume,
        include_aws_internal=args.include_aws_internal,
        filters=args.filters,
    )
    DtoApp(service, state, tag_key=None if args.group_by else args.tag_key).run()


if __name__ == "__main__":
    main()
