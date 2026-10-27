# DTO TUI

A terminal UI that totals **AWS Data Transfer Out (DTO)** cost and volume for a week, a month or a custom date range, and breaks it down by region, account, transfer type, service, usage type, cost allocation tag (for example a VPC tag) or cost category.

It's a single self-contained Python script ([`dto_tui.py`](dto_tui.py)) built on [Textual](https://textual.textualize.io/) and boto3, with AWS Cost Explorer as the data source. It's a terminal port of the [DTO Explorer](https://github.com/zscclabs/dto-explorer) web app and gives the same numbers.

![DTO TUI: KPIs, cost and volume sparklines, share by transfer type and a breakdown by VPC tag](docs/screenshot.png)

## Features
- **Periods:** weekly (Monday–Sunday) or monthly with previous/next navigation, or a custom range with presets (last 7/30/90 days, year to date, last 12 months)
- **Headline numbers:** total cost, total volume, cost per GB and top contributor. Cost and volume show the change against the previous period of the same length.
- **Charts and table:** cost and volume sparklines, share by transfer type, and a sortable, searchable table with drill-down
- **Filters:** account, region and transfer type, plus drill-down filters on any dimension
- **Other:** CSV export, a choice of cost metric (unblended, amortized, net and so on), a [discounts and credits](#discounts-and-credits) switch, and a mock-data mode that needs no AWS account

## Quick start
You need [uv](https://docs.astral.sh/uv/). The script declares its dependencies inline ([PEP 723](https://peps.python.org/pep-0723/)), so uv installs them on first run, and you can copy the file anywhere.

```bash
curl -O https://raw.githubusercontent.com/zscclabs/dto/main/dto_tui.py

uv run dto_tui.py --mock --tag-key vpc          # synthetic data, no AWS needed

aws sso login --profile my-profile               # or any other way of getting credentials
uv run dto_tui.py --profile my-profile           # real data
```

Start in a particular view from the command line:
```bash
uv run dto_tui.py --period custom --start 2026-07-01 --end 2026-09-30 \
  --group-by LINKED_ACCOUNT --filter REGION=us-east-1 --filter TAG:vpc=prod
```
Filters for the same dimension are OR'd and different dimensions are AND'd. An empty tag value (`TAG:vpc=`) means untagged. Run `uv run dto_tui.py --help` for every option.

### Keys
| Key | Action |
|---|---|
| `[` / `]` | Previous / next period |
| `f` | Filters (account, region, transfer type) |
| `Enter` | Drill into the selected row |
| `Backspace` | Back out of the last drill-down |
| `x` | Clear filters |
| `/` | Search rows |
| `s`, or click a header | Sort |
| `e` | Export CSV to the current directory |
| `r` | Clear the cache and refresh |
| `d` | Light / dark theme |
| `?` | Help |
| `q` | Quit |

`Tab` moves between the period, group-by, metric and discounts controls.

### Single account vs. AWS Organization
- **Single account:** use that account's credentials. The account filter shows just that one account.
- **Organization:** use credentials for the **management (payer) account** to see every linked account. Breaking down by account then shows each account by name.

## Configuration
Each option can also come from an environment variable, which the flag overrides.

| Flag | Variable | Default | Purpose |
|---|---|---|---|
| `--profile` | `DTO_AWS_PROFILE` | – | AWS profile; otherwise the standard boto3 chain (including `AWS_PROFILE`) |
| `--mock` | `DTO_MOCK` | off | Synthetic data instead of calling AWS |
| `--cache-ttl` | `DTO_CACHE_TTL_SECONDS` | `3600` | Cache lifetime; `0` disables caching |
| `--tag-key` | `DTO_DEFAULT_TAG_KEY` | – | Tag key to group by initially (e.g. `vpc`) |
| `--ce-region` | `DTO_CE_REGION` | `us-east-1` | Cost Explorer endpoint region |

## AWS setup

### IAM permissions
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["ce:GetCostAndUsage", "ce:GetDimensionValues", "ce:GetTags", "ce:GetCostCategories"],
    "Resource": "*"
  }]
}
```

### Cost
Each Cost Explorer request costs **$0.01**. One view makes about 12 requests on a cold cache. Responses are cached in memory for 1 hour (`--cache-ttl`). `r` clears the cache and fetches fresh data.

### Discounts and credits
By default the TUI shows **usage charges at list price** (Cost Explorer `RECORD_TYPE = Usage`). AWS books credits, refunds and EDP / private-rate discounts as separate negative charges, often under no region, so including them would produce negative groups and shares above 100%. Turn on **Include discounts & credits** (or pass `--include-discounts`) to see net cost instead. Volume (GB) is the same either way.

### Per-VPC breakdown
Cost Explorer has **no VPC dimension**. To get a per-VPC breakdown:
1. Tag resources that generate transfer charges (NAT gateways, load balancers, EC2 instances, etc.) with something like `vpc=<name>`. An existing `Name` tag works if your resources are named after their VPC.
2. Activate that tag as a **cost allocation tag** in Billing → Cost allocation tags. In an Organization, only the management account can do this. Data appears from the activation date onward.
3. Choose **Tag: vpc** as the breakdown, or start with `--tag-key vpc`.

## How DTO is identified
Usage types are discovered with `GetDimensionValues` and then classified:

| Transfer type | Usage type pattern |
|---|---|
| Internet Out | `*DataTransfer-Out-Bytes` (includes CloudFront edge egress) |
| Inter-Region | `<SRC>-<DST>-AWS-Out-Bytes` |
| Inter-AZ | `*DataTransfer-Regional-Bytes` |
| NAT Gateway | `*NatGateway-Bytes` (data processing) |
| To CloudFront | `*CloudFront-Out-Bytes` |
| Other Out | anything else ending in `Out-Bytes`, `Out-OBytes` or `Out-ABytes` |

Inbound usage types (`*-In-*`) are excluded.

## Development
```bash
uv sync               # dev environment (.venv) with textual, boto3 and pytest
uv run pytest -q      # tests: classification, periods, summaries on mock data, headless UI runs
uv run dto_tui.py --mock
```

The script keeps the sections of the DTO Explorer backend it was ported from (periods, cache, Cost Explorer client, mock client, models, DTO service), each labelled with its source file. When changing classification or query logic there, port the change here too. CI (`.github/workflows/ci.yml`) runs the tests on Python 3.11 and 3.13 and checks that the script runs from its inline metadata.

The version lives in `pyproject.toml` and `__version__` in `dto_tui.py`, which must match. Dependencies are listed both in `pyproject.toml` and in the script's PEP 723 header.

## License
MIT (see [LICENSE](LICENSE)).
