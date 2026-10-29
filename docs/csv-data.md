# Reading data from CSV files

Instead of calling Cost Explorer live, DTO TUI can read data transfer usage from CSV files. Export the data once with the AWS CLI, then run `uv run dto_tui.py --csv PATH` with the file or folder. The TUI then needs no AWS credentials and makes no paid Cost Explorer requests, and you can share the files with people who have no access to the billing account. The format is the same as [DTO Explorer](https://github.com/zscclabs/dto-explorer)'s, so the same files work in both.

- [CSV format](#csv-format)
- [Exporting with the AWS CLI](#exporting-with-the-aws-cli)
- [Running the TUI on CSV data](#running-the-tui-on-csv-data)
- [Combining files](#combining-files)
- [Troubleshooting](#troubleshooting)

## CSV format

One row per day (or month), usage type and whatever dimensions you exported. The header row names the columns; order doesn't matter, names are case-insensitive, and unknown columns are ignored.

| Column | Required | Contents |
|---|---|---|
| `date` | yes | `YYYY-MM-DD`, the start of the day (or of the month for monthly data) |
| `usage_type` | yes | Cost Explorer usage type, e.g. `USE1-DataTransfer-Out-Bytes` |
| `usage_quantity` | yes | Volume in GB |
| `region` | no | Region code, e.g. `us-east-1` |
| `linked_account` | no | 12-digit account ID |
| `linked_account_name` | no | Account name shown instead of the ID |
| `service` | no | Service name, e.g. `EC2 - Other` |
| `record_type` | no | Defaults to `Usage`; rows with any other value are ignored |
| `tag:<key>` | no | Cost allocation tag value, e.g. a `tag:vpc` column; empty means untagged |
| `cost_category:<name>` | no | Cost category value, e.g. `cost_category:Business Unit` |

```csv
date,linked_account,linked_account_name,usage_type,region,usage_quantity
2026-09-01,111111111111,prod,USE1-DataTransfer-Out-Bytes,us-east-1,812.4
2026-09-01,111111111111,prod,USE1-DataTransfer-In-Bytes,us-east-1,1290.7
2026-09-01,222222222222,staging,EU-DataTransfer-Out-Bytes,eu-west-1,95.0
```

Usage types are classified exactly as with live data (see [How traffic is classified](../README.md#how-traffic-is-classified)); rows for usage types that aren't data transfer are ignored. Rows that repeat the same day and dimensions are added together.

Breakdowns and filters follow the columns: without a `region` column the Region breakdown and filter are hidden, and so on. If the TUI starts on a breakdown the files don't have (Region by default), it switches to Transfer type. Transfer type, direction and usage type are always available because they come from `usage_type`.

## Exporting with the AWS CLI

### What you need
- AWS CLI v2 and [jq](https://jqlang.org/)
- Credentials for the account to export. In an AWS Organization, use the **management (payer) account** to get every linked account.
- IAM permissions `ce:GetCostAndUsage` and `ce:GetDimensionValues`
- Each Cost Explorer request costs **$0.01**

### The export script

[`scripts/export-ce-csv.sh`](../scripts/export-ce-csv.sh) writes the CSV to stdout:

```bash
aws sso login --profile my-profile
export AWS_PROFILE=my-profile

mkdir -p data
# September 2026, by account and region (END is exclusive)
scripts/export-ce-csv.sh 2026-09-01 2026-10-01 > data/2026-09.csv

# By account and VPC tag instead of region
scripts/export-ce-csv.sh 2026-09-01 2026-10-01 TAG:vpc > data/2026-09-vpc.csv
```

| Argument / variable | Meaning |
|---|---|
| `START END` | `YYYY-MM-DD`; `END` is exclusive |
| third argument | Second breakdown next to usage type: `REGION` (default), `SERVICE`, `TAG:<key>` or `COST_CATEGORY:<name>` |
| `GRANULARITY` | `DAILY` (default) or `MONTHLY` |
| `ACCOUNTS` | Space-separated account IDs to export, e.g. `ACCOUNTS="111111111111 222222222222"`; default is every linked account |

What it does:
1. Finds the data transfer usage types in the window with `aws ce get-dimension-values --dimension USAGE_TYPE --search-string …`, using the same search strings as the TUI (8 requests).
2. Lists the linked accounts and their names with `aws ce get-dimension-values --dimension LINKED_ACCOUNT`.
3. For each account, runs `aws ce get-cost-and-usage` for `UsageQuantity`, filtered to that account, `RECORD_TYPE = Usage` and those usage types, and grouped by `USAGE_TYPE` and the second breakdown. It follows `NextPageToken` until every page is read.
4. Converts the results to CSV with `jq` and prints the number of requests it made to stderr.

Cost Explorer allows only two `GroupBy` keys per request. Running one request per account adds a third dimension (account) without spending a group key, so a default export gives you account, region and usage type.

Cost: roughly 9 requests plus one per account (more when results are paged), so about $0.30 for an organization with 20 accounts.

### One request, no script

For a single account or a quick look, one `get-cost-and-usage` call and a `jq` filter are enough. This groups by usage type and region across all accounts:

```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-09-01,End=2026-10-01 \
  --granularity DAILY \
  --metrics UsageQuantity \
  --filter '{"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}' \
  --group-by Type=DIMENSION,Key=USAGE_TYPE Type=DIMENSION,Key=REGION \
  --output json \
| jq -r '
    ["date", "usage_type", "region", "usage_quantity"],
    (.ResultsByTime[] | .TimePeriod.Start as $d | .Groups[]
      | select(.Keys[0] | test("Bytes"))
      | [$d, .Keys[0], .Keys[1], .Metrics.UsageQuantity.Amount])
    | @csv' > data/usage.csv
```

Two things to watch:
- Without a usage type filter the response contains every service's usage, so it pages a lot. If the output ends with a `NextPageToken`, call again with `--next-page-token <token>` and append the rows without the header (or use the script, which does this for you).
- Tag and cost category groups come back as `key$value`. Strip the prefix in `jq` with `(.Keys[1] | sub("^[^$]*\\$"; ""))` and name the column `tag:<key>` or `cost_category:<name>`.

### History limits
Cost Explorer keeps daily data for about the last 14 months. For older periods use `GRANULARITY=MONTHLY`; each row is then dated the first of its month. Monthly rows work in the month view and in custom ranges, but daily charts show the whole month as one bar on the 1st.

## Running the TUI on CSV data

`--csv` takes a single `.csv` file or a folder; for a folder, every `*.csv` file directly inside it is read. You can also set it with the `DTO_CSV_PATH` environment variable. It can't be combined with `--mock`.

```bash
uv run dto_tui.py --csv ./data                   # every *.csv file in ./data
uv run dto_tui.py --csv ./data/2026-09.csv --period month --start 2026-09-01 --tag-key vpc
```

The subtitle shows **CSV data**, and a notification on start lists the files read, the number of rows and the first and last date in the data. If a file has errors, the TUI exits with a `file:line` message before it opens. After re-exporting, press `r` to read the files again; if the new files have errors, the TUI keeps the data it already had and shows the error.

The TUI opens on the current month. If your export covers an earlier period, move back with `[` or start with `--start` (and `--period`), or pick a custom range.

## Combining files

All files in the folder are added together, so **files must not overlap**. Split exports by period or by account, never by dimension:

- ✅ `2026-08.csv` and `2026-09.csv`, both by account and region
- ✅ `prod.csv` (`ACCOUNTS=111111111111`) and `others.csv` (the remaining accounts) for the same month
- ❌ `2026-09-region.csv` and `2026-09-vpc.csv`: every byte is counted twice

To look at the same period by region and by VPC tag, keep two folders and run the TUI with whichever one you need.

## Troubleshooting

| Message | Fix |
|---|---|
| `missing required column(s)` | The header needs `date`, `usage_type` and `usage_quantity`. |
| `date must be YYYY-MM-DD` | Spreadsheet apps often reformat dates; export as plain CSV with ISO dates. |
| `usage_quantity must be a number` | Remove thousands separators and units. |
| `no .csv files in directory` | Files must end in `.csv` and sit directly in the folder (subfolders aren't read). |
| Everything is zero | The selected period isn't in the files; check the date range in the notification shown on start and after `r`. |
| Region / Account breakdown missing | The file has no `region` / `linked_account` column. |
