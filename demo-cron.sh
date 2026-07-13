#!/usr/bin/env bash
# Demo the auto-crawl scheduler end to end: show the schedule, prove the endpoint
# is fail-shut, trigger the sweep, then TAIL THE LOGS so you can watch it run.
# Reads CRON_SECRET from the gitignored terraform.tfvars so nothing secret is typed.
#
#   ./demo-cron.sh
#
set -euo pipefail

API="https://api.llmstextgeneratorrishishah.com"
REGION="us-east-2"
LOG_GROUP="/ecs/llms-backend"
HERE="$(cd "$(dirname "$0")" && pwd)"
SECRET="$(grep '^cron_secret' "$HERE/terraform/terraform.tfvars" | sed 's/.*= *"\(.*\)"/\1/')"

echo
echo "── 1. The schedule is real and live ──────────────────────────────"
aws events list-rules --region "$REGION" \
  --query "Rules[?Name=='llms-nightly-refresh'].{name:Name,schedule:ScheduleExpression,state:State}" \
  --output table

echo
echo "── 2. Fail-shut: no secret is rejected ───────────────────────────"
echo "\$ curl -X POST $API/internal/cron/refresh"
curl -s -o /dev/null -w "   → HTTP %{http_code}  (Unauthorized)\n" \
  -X POST "$API/internal/cron/refresh"

echo
echo "── 3. With the shared secret, the nightly sweep is triggered ─────"
echo "\$ curl -X POST $API/internal/cron/refresh -H 'X-Cron-Secret: ••••••'"
curl -s -w "   → HTTP %{http_code}\n" \
  -X POST "$API/internal/cron/refresh" -H "X-Cron-Secret: $SECRET"

echo
echo "── 4. Watch the sweep actually run (its own log lines) ───────────"
echo "   tailing $LOG_GROUP for 'refresh sweep' ..."
# The sweep runs in the background after the 200, so give it a moment. Use a tight
# ~15s window (not 1m) so re-running the script doesn't also show the PREVIOUS
# run's sweep -- each trigger fires exactly one sweep, and this shows only the one
# you just triggered.
sleep 6
aws logs tail "$LOG_GROUP" --region "$REGION" --since 15s 2>/dev/null \
  | grep "refresh sweep" \
  | sed 's/.*refresh sweep/   refresh sweep/' \
  || echo "   (no due sites this run -- see note below)"

cat <<'NOTE'

   NOTE: "N site(s) due" reflects what is actually due right now. A site enrolled
   today isn't due until the next midnight UTC, so a fresh run will honestly say
   "0 due". That is correct behaviour -- the point is that the schedule, the auth,
   and the sweep all run and are now observable. To show the sweep re-crawl a site
   live, one enrolled row's next_check_at has to be backdated first.
NOTE
