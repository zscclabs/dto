# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-10-29

Ported from [DTO Explorer](https://github.com/zscclabs/dto-explorer) v0.3.0 and its CSV data source.

### Changed (breaking)
- The TUI reports data transfer **volume only**. Cost, the cost metric choice, the discounts switch, cost per GB and the `--metric` and `--include-discounts` options are gone. The table, KPIs and transfer type panel show GB.
- "Inter-Region" is renamed "Inter-Region Out".
- CSV export columns are `<breakdown>, label, gb, pct, gb_<transfer type>…`.
- By default only internet traffic is counted. Inter-AZ, Inter-Region Out/In and To CloudFront are hidden behind an **Include traffic within AWS** switch (`--include-aws-internal`).

### Added
- Inbound traffic: Internet In (`*DataTransfer-In-Bytes`), Inter-Region In (`*-AWS-In-Bytes`) and Other In.
- Direction (Out / In / Transit) as a filter and a breakdown (`DIRECTION`), with egress, ingress and transit totals and their change against the previous period.
- Egress, Ingress and Transit KPI tiles and egress / ingress sparklines. Inter-AZ, NAT Gateway and To CloudFront are labelled as transit.
- **Include NAT volume** switch (`--include-nat-volume`) to add NAT Gateway data processed back into transit.
- CSV data source: `--csv PATH` (or `DTO_CSV_PATH`) reads data transfer usage from a CSV file, or every `*.csv` file in a folder, instead of calling Cost Explorer. Same format and `file:line` validation errors as DTO Explorer. Breakdowns and filters follow the columns present (region, account, service, `tag:<key>`, `cost_category:<name>`). The subtitle shows "CSV data" and `r` re-reads the files, keeping the old data if the new files are invalid. See [docs/csv-data.md](docs/csv-data.md).
- `scripts/export-ce-csv.sh` exports that CSV with the AWS CLI and jq.

### Fixed
- Data volume no longer double-counts NAT Gateway traffic: NAT data processed is left out of every total by default.

## [0.1.0] - 2026-10-27

### Added
- Standalone Textual TUI for AWS Data Transfer Out, ported from [DTO Explorer](https://github.com/zscclabs/dto-explorer) v0.2.0: week / month / custom periods with presets, breakdowns by region, account, service, transfer type, usage type, tag or cost category, metric and discounts switches, filters, drill-down, KPIs with change against the previous period, cost and volume sparklines, sortable and searchable table, CSV export and a `--mock` mode.
