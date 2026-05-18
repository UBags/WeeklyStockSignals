#!/usr/bin/env bash
#
# Fast smoke test for trip_ema.
#
# What this does:
#   - Assumes trip-ema service is already running on localhost:8000.
#   - Hits /healthz and asserts cache_size > 0.
#   - Hits the history endpoint for one tier-1 ticker and asserts bars come back.
#
# What this does NOT do:
#   - Start/stop the service.
#   - Touch the database except to read tier-1 ticker rows.
#   - Bootstrap anything.
#
# Total runtime: a few seconds. Run after any code change to confirm
# the running install still serves valid signals.
#
# Prereqs:
#   - ~/.pgpass contains: localhost:5432:*:trip_ema_app:<password>
#   - trip-ema service running and reachable on localhost:8000.

set -euo pipefail

SERVICE_PORT=8000
PG_HOST="localhost"
PG_USER="trip_ema_app"
PG_DB="trip_ema"
HEALTH_URL="http://localhost:${SERVICE_PORT}/healthz"

echo "=== FAST smoke test ==="

# Step 1: confirm service is reachable. If it's not running, fail
# clearly with a useful message rather than a curl error.
if ! curl -fsS "$HEALTH_URL" > /dev/null 2>&1; then
    echo "FAIL: cannot reach $HEALTH_URL"
    echo "Is trip-ema running? Start it with:"
    echo "  trip-ema --config config.yaml"
    exit 1
fi

# Check 1: cache_size assertion.
# This catches Issue A's failure mode — priming silently skipped
# every ticker because tickers.source_timeframe is stale relative
# to what ohlcv_bars actually contains.
echo ""
echo "Check 1: /healthz cache_size > 0"
response=$(curl -fsS "$HEALTH_URL")
echo "  $response"
cache_size=$(echo "$response" | python -c "import sys, json; print(json.load(sys.stdin).get('cache_size', 0))")
if [[ "$cache_size" -lt 1 ]]; then
    echo "FAIL: cache_size is $cache_size, expected >= 1"
    echo ""
    echo "This is Issue A's failure mode: priming silently skipped"
    echo "tickers. Look at how tickers.source_timeframe relates to"
    echo "what's in ohlcv_bars for tier-1 tickers."
    exit 1
fi
echo "  PASS — cache_size = $cache_size"

# Check 2: history endpoint. Pick a tier-1 ticker dynamically from
# the DB rather than hardcoding it — that way the script keeps
# working if your tier-1 set changes.
echo ""
echo "Check 2: history endpoint returns bars"
ticker_row=$(psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" -tA -F'|' -c \
    "SELECT exchange, symbol FROM tickers WHERE tier = 1 LIMIT 1;")

if [[ -z "$ticker_row" ]]; then
    echo "FAIL: no tier-1 tickers found in DB. Run:"
    echo "  trip-ema-etl refresh-listings"
    echo "  psql ... UPDATE tickers SET tier = 1 WHERE symbol IN (...)"
    exit 1
fi

IFS='|' read -r EX SYM <<< "$ticker_row"
hist_url="http://localhost:${SERVICE_PORT}/v1/signals/${EX}/${SYM}/history?years=1"
echo "  GET $hist_url"
response=$(curl -fsS "$hist_url")
bars_count=$(echo "$response" | python -c "import sys, json; print(len(json.load(sys.stdin).get('bars', [])))")
if [[ "$bars_count" -lt 1 ]]; then
    echo "FAIL: history endpoint returned $bars_count bars"
    echo ""
    echo "Response (first 500 chars):"
    echo "$response" | head -c 500
    echo ""
    echo ""
    echo "If cache_size was > 0 but this returned no bars, suspect:"
    echo "  - The signal_type vs signal SQL bug (briefing item 11)"
    echo "  - The discovery-based source timeframe lookup in signals.py"
    echo "  - Bars exist for this ticker but at a timeframe the route"
    echo "    doesn't know to aggregate from"
    exit 1
fi
echo "  PASS — bars_count = $bars_count"

echo ""
echo "=== FAST smoke test PASSED ==="