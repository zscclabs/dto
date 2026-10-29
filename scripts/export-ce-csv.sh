#!/usr/bin/env bash
# Export data transfer usage from AWS Cost Explorer as CSV for `dto_tui.py --csv` (see docs/csv-data.md).
#
#   scripts/export-ce-csv.sh START END [GROUP2] > usage.csv
#
#   START, END  YYYY-MM-DD; END is exclusive (2026-09-01 2026-10-01 = September)
#   GROUP2      second breakdown next to the usage type: REGION (default), SERVICE,
#               TAG:<key> or COST_CATEGORY:<name>
#
# Environment:
#   AWS_PROFILE etc.  standard AWS CLI credentials
#   GRANULARITY       DAILY (default) or MONTHLY
#   ACCOUNTS          space-separated account IDs to export (default: every linked account)
#
# Needs the AWS CLI v2 and jq. Every Cost Explorer request costs $0.01; the total is
# printed to stderr when the export finishes.
set -euo pipefail

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
[[ $# -ge 2 && $# -le 3 ]] || usage
START=$1 END=$2 GROUP2=${3:-REGION}
GRANULARITY=${GRANULARITY:-DAILY}
for d in "$START" "$END"; do
  [[ $d =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "error: dates must be YYYY-MM-DD, got '$d'" >&2; exit 2; }
done
command -v aws >/dev/null || { echo "error: the AWS CLI is not installed" >&2; exit 1; }
command -v jq >/dev/null || { echo "error: jq is not installed" >&2; exit 1; }

# GROUP2 -> Cost Explorer group definition and CSV column name.
case $GROUP2 in
  REGION | SERVICE) GROUP_JSON=$(jq -nc --arg k "$GROUP2" '{Type: "DIMENSION", Key: $k}'); COLUMN=$(tr '[:upper:]' '[:lower:]' <<<"$GROUP2") ;;
  TAG:?*) GROUP_JSON=$(jq -nc --arg k "${GROUP2#TAG:}" '{Type: "TAG", Key: $k}'); COLUMN="tag:${GROUP2#TAG:}" ;;
  COST_CATEGORY:?*) GROUP_JSON=$(jq -nc --arg k "${GROUP2#COST_CATEGORY:}" '{Type: "COST_CATEGORY", Key: $k}'); COLUMN="cost_category:${GROUP2#COST_CATEGORY:}" ;;
  *) echo "error: GROUP2 must be REGION, SERVICE, TAG:<key> or COST_CATEGORY:<name>, got '$GROUP2'" >&2; exit 2 ;;
esac

# Requests are counted in a file because dimension_values runs in subshells.
COUNTER=$(mktemp)
trap 'rm -f "$COUNTER"' EXIT

# All values of a dimension in the window (one JSON object per line: {Value, Attributes}).
dimension_values() { # DIMENSION [SEARCH]
  local token="" resp
  local args=(--time-period "Start=$START,End=$END" --dimension "$1" --context COST_AND_USAGE --output json)
  [[ -n ${2:-} ]] && args+=(--search-string "$2")
  while :; do
    resp=$(aws ce get-dimension-values "${args[@]}" ${token:+--next-page-token "$token"})
    echo >>"$COUNTER"
    jq -c '.DimensionValues[]' <<<"$resp"
    token=$(jq -r '.NextPageToken // empty' <<<"$resp")
    [[ -n $token ]] || break
  done
}

# Data transfer usage types, found the same way the app finds them.
USAGE_TYPES=$(
  for s in DataTransfer Out-Bytes Out-OBytes Out-ABytes In-Bytes In-OBytes In-ABytes NatGateway-Bytes; do
    dimension_values USAGE_TYPE "$s"
  done | jq -sc '[.[].Value | select(test("Bytes"))] | unique'
)
if [[ $USAGE_TYPES == "[]" ]]; then
  echo "error: no data transfer usage types between $START and $END" >&2
  exit 1
fi

ACCOUNT_JSON=$(dimension_values LINKED_ACCOUNT | jq -sc 'map({key: .Value, value: (.Attributes.description // "")}) | from_entries')
read -r -a ACCOUNT_IDS <<<"${ACCOUNTS:-$(jq -r 'keys | join(" ")' <<<"$ACCOUNT_JSON")}"

jq -rn --arg col "$COLUMN" '["date", "linked_account", "linked_account_name", "usage_type", $col, "usage_quantity"] | @csv'

for account in "${ACCOUNT_IDS[@]}"; do
  name=$(jq -r --arg a "$account" '.[$a] // ""' <<<"$ACCOUNT_JSON")
  filter=$(jq -nc --arg a "$account" --argjson ut "$USAGE_TYPES" '{And: [
    {Dimensions: {Key: "LINKED_ACCOUNT", Values: [$a]}},
    {Dimensions: {Key: "RECORD_TYPE", Values: ["Usage"]}},
    {Dimensions: {Key: "USAGE_TYPE", Values: $ut}}
  ]}')
  group_by=$(jq -nc --argjson g "$GROUP_JSON" '[{Type: "DIMENSION", Key: "USAGE_TYPE"}, $g]')
  token=""
  while :; do
    resp=$(aws ce get-cost-and-usage --output json \
      --time-period "Start=$START,End=$END" --granularity "$GRANULARITY" --metrics UsageQuantity \
      --filter "$filter" --group-by "$group_by" ${token:+--next-page-token "$token"})
    echo >>"$COUNTER"
    # Tag and cost category keys come back as "key$value"; keep the value.
    jq -r --arg a "$account" --arg n "$name" '
      .ResultsByTime[] | .TimePeriod.Start as $d | .Groups[]
      | [$d, $a, $n, .Keys[0], (.Keys[1] | sub("^[^$]*\\$"; "")), .Metrics.UsageQuantity.Amount]
      | @csv' <<<"$resp"
    token=$(jq -r '.NextPageToken // empty' <<<"$resp")
    [[ -n $token ]] || break
  done
  echo "exported account $account${name:+ ($name)}" >&2
done

REQUESTS=$(wc -l <"$COUNTER" | tr -d ' ')
echo "done: $REQUESTS Cost Explorer requests (about \$$(printf '%d.%02d' $((REQUESTS / 100)) $((REQUESTS % 100))))" >&2
