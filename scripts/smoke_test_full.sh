#!/usr/bin/env bash
#
# Full smoke test for trip_ema.
#
# Bootstraps a throwaway trip_ema_smoke DB from zero:
#   1. Drop+create trip_ema_smoke DB (as postgres superuser).
#   2. Run alembic upgrade head against it.
#   3. Run trip-ema-etl refresh-listings.
#   4. Promote a handful of tickers to tier 1.
#   5. Run trip-ema-etl backfill --tier 1.
#   6. Start trip-ema on an alternate port.
#   7. Hit /healthz, assert cache_size > 0.
#   8. Hit history endpoint, assert bars come back.
#   9. Stop service, clean up (DB drop) unless --keep is passed.
#
# Total runtime: 5-10 minutes, depending on refresh-listings speed.
#
# Prereqs:
#   - POSTGRES_PASSWORD env var set to the postgres superuser password.
#   - trip_ema_app exists as a role (it does — created by 0002_db_roles).
#   - trip_ema_app's password is 'devpassword' in ~/.pgpass already.
#   - trip-ema service is NOT currently bound to port 8001.
#
# Usage:
#   POSTGRES_PASSWORD='...' ./scripts/smoke_test_full.sh
#   POSTGRES_PASSWORD='...' ./scripts/smoke_test_full.sh --keep
#
# --keep preserves the throwaway DB even on success, useful while
# iterating on the script itself.

set -euo pipefail

KEEP=0
if [[ "${1:-}" == "--keep" ]]; then
    KEEP=1
fi

# === Configuration ===
SERVICE_PORT=8001                          # alternate port to avoid main install
PG_HOST="localhost"
PG_SUPERUSER="postgres"
PG_APP_USER="trip_ema_app"
DB_NAME="trip_ema_smoke"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CONFIG=/tmp/trip_ema_smoke_config.yaml
PID_FILE=/tmp/trip-ema-smoke.pid
LOG_FILE=/tmp/trip-ema-smoke.log

HEALTH_URL="http://localhost:${SERVICE_PORT}/healthz"

# === Validate environment ===
#if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
#    echo "FAIL: POSTGRES_PASSWORD env var not set."
#    echo "Usage: POSTGRES_PASSWORD='...' $0"
#    exit 2
#fi

# === Helpers ===
psql_super() {
    psql -h "$PG_HOST" -U "$PG_SUPERUSER" "$@"
}

psql_app() {
    # Uses ~/.pgpass for credentials
    psql -h "$PG_HOST" -U "$PG_APP_USER" "$@"
}

# === Cleanup trap ===
cleanup() {
    if [[ -f "$PID_FILE" ]]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
    if [[ "${SMOKE_SUCCESS:-0}" == "1" && "$KEEP" == "0" ]]; then
        psql_super -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};" >/dev/null 2>&1 || true
        rm -f "$CONFIG"
        echo "(cleaned up throwaway DB ${DB_NAME})"
    elif [[ "$KEEP" == "1" ]]; then
        echo "(--keep: throwaway DB ${DB_NAME} preserved)"
        echo "(config preserved at $CONFIG)"
    elif [[ "${SMOKE_SUCCESS:-0}" != "1" ]]; then
        echo ""
        echo "(smoke test failed; throwaway DB ${DB_NAME} preserved for inspection)"
        echo "(service log at $LOG_FILE)"
        echo "(to clean up manually: PGPASSWORD='...' psql -h localhost -U postgres -d postgres -c 'DROP DATABASE trip_ema_smoke;')"
    fi
}
trap cleanup EXIT

echo "=== FULL smoke test ==="
echo ""

# === Step 1: drop+create throwaway DB ===
echo "Step 1: create throwaway DB ${DB_NAME}"
psql_super -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};" >/dev/null
psql_super -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER ${PG_APP_USER};" >/dev/null
echo "  created"

# === Step 2: write smoke-test config ===
echo ""
echo "Step 2: write smoke-test config"
# Start from the actual working config.yaml so we inherit all the
# already-correct fields (paths, intervals, observability settings).
# Override only what needs to differ.
cp config.yaml "$CONFIG"

# Patch the database name to point at the throwaway DB.
sed -i "s/^  database: trip_ema$/  database: ${DB_NAME}/" "$CONFIG"

# Patch the HTTP port to the alternate.
sed -i "s/^  port: 8000$/  port: ${SERVICE_PORT}/" "$CONFIG"

# Sanity-check the patches landed.
grep "database: ${DB_NAME}" "$CONFIG" > /dev/null || {
    echo "FAIL: config patch for database didn't apply"
    grep "database:" "$CONFIG"
    exit 1
}
grep "port: ${SERVICE_PORT}" "$CONFIG" > /dev/null || {
    echo "FAIL: config patch for port didn't apply"
    grep "port:" "$CONFIG"
    exit 1
}
echo "  patched: database=${DB_NAME}, port=${SERVICE_PORT}"

# === Step 3: alembic upgrade head ===
echo ""
echo "Step 3: alembic upgrade head"
# Alembic's connection comes from alembic.ini, which itself reads
# from somewhere — likely an env var or the working config. Check
# how it's wired in the repo:
#   grep sqlalchemy.url alembic.ini
# For now we set both env vars commonly used, and let alembic pick.
TRIP_EMA_CONFIG="$CONFIG" \
TRIP_EMA_DB_PASSWORD="devpassword" \
    alembic -c alembic.ini upgrade head

# === Step 4: confirm exactly one alembic head ===
echo ""
echo "Step 4: confirm exactly one alembic head"
head_count=$(alembic -c alembic.ini heads 2>/dev/null | grep -c "(head)" || true)
if [[ "$head_count" -ne 1 ]]; then
    echo "FAIL: alembic has $head_count heads, expected 1"
    alembic -c alembic.ini heads
    exit 1
fi
echo "  PASS — exactly 1 head"

# === Step 5: refresh-listings ===
echo ""
echo "Step 5: refresh-listings (this is the slow step; 1-3 min)"
TRIP_EMA_DB_PASSWORD="devpassword" \
    trip-ema-etl --config "$CONFIG" refresh-listings

# === Step 6: promote tier-1 tickers ===
echo ""
echo "Step 6: promote tier-1 tickers"
# Use NSE Indian tickers since that's what your refresh-listings populates.
# Adjust this list if refresh-listings creates a different set.
psql_app -d "$DB_NAME" -c \
    "UPDATE tickers SET tier = 1 WHERE exchange = 'NSE' AND symbol IN ('RELIANCE','TCS','INFY','HDFCBANK','ICICIBANK');"

promoted=$(psql_app -d "$DB_NAME" -tAc "SELECT COUNT(*) FROM tickers WHERE tier = 1;")
echo "  promoted = $promoted"
if [[ "$promoted" -lt 1 ]]; then
    echo "FAIL: no tickers got promoted to tier 1."
    echo ""
    echo "What refresh-listings actually created (first 10):"
    psql_app -d "$DB_NAME" -c "SELECT exchange, symbol, instrument FROM tickers LIMIT 10;"
    echo ""
    echo "Adjust the UPDATE in step 6 of this script to match."
    exit 1
fi

# === Step 7: backfill ===
echo ""
echo "Step 7: backfill 60 days (1-3 min)"
TRIP_EMA_DB_PASSWORD="devpassword" \
    trip-ema-etl --config "$CONFIG" backfill \
        --tier 1 \
        --start "$(date -d '60 days ago' +%Y-%m-%d)"

# === Step 8: assert bars were written ===
echo ""
echo "Step 8: assert bars were written"
bar_count=$(psql_app -d "$DB_NAME" -tAc "SELECT COUNT(*) FROM ohlcv_bars;")
echo "  bar_count = $bar_count"
if [[ "$bar_count" -lt 10 ]]; then
    echo "FAIL: backfill wrote $bar_count bars, expected >= 10"
    exit 1
fi

# === Step 9: start service ===
echo ""
echo "Step 9: start trip-ema service on port ${SERVICE_PORT}"
TRIP_EMA_DB_PASSWORD="devpassword" \
    trip-ema --config "$CONFIG" > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

echo "Waiting for /healthz to become reachable..."
for i in {1..60}; do
    if curl -fsS "$HEALTH_URL" > /dev/null 2>&1; then
        echo "  reachable after ${i}s"
        break
    fi
    if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "FAIL: trip-ema died during startup. Log tail:"
        tail -50 "$LOG_FILE"
        exit 1
    fi
    sleep 1
    if [[ $i -eq 60 ]]; then
        echo "FAIL: /healthz never became reachable. Log tail:"
        tail -50 "$LOG_FILE"
        exit 1
    fi
done

# === Step 10: cache_size check ===
echo ""
echo "Step 10: /healthz cache_size > 0"
response=$(curl -fsS "$HEALTH_URL")
echo "  $response"
cache_size=$(echo "$response" | python -c "import sys, json; print(json.load(sys.stdin).get('cache_size', 0))")
if [[ "$cache_size" -lt 1 ]]; then
    echo "FAIL: cache_size is $cache_size, expected >= 1"
    echo ""
    echo "This is Issue A's smoking gun: bars exist in ohlcv_bars"
    echo "(we just confirmed $bar_count of them) but priming skipped"
    echo "every ticker. Compare tickers.source_timeframe values"
    echo "to ohlcv_bars.timeframe values:"
    echo ""
    echo "  psql -d $DB_NAME -c \\"
    echo "    'SELECT DISTINCT source_timeframe FROM tickers WHERE tier=1;'"
    echo "  psql -d $DB_NAME -c \\"
    echo "    'SELECT DISTINCT timeframe FROM ohlcv_bars;'"
    exit 1
fi
echo "  PASS — cache_size = $cache_size"

# === Step 11: history endpoint check ===
echo ""
echo "Step 11: history endpoint returns bars"
ticker_row=$(psql_app -d "$DB_NAME" -tA -F'|' -c \
    "SELECT exchange, symbol FROM tickers WHERE tier = 1 LIMIT 1;")
IFS='|' read -r EX SYM <<< "$ticker_row"
hist_url="http://localhost:${SERVICE_PORT}/v1/signals/${EX}/${SYM}/history?years=1"
echo "  GET $hist_url"
response=$(curl -fsS "$hist_url")
bars_count=$(echo "$response" | python -c "import sys, json; print(len(json.load(sys.stdin).get('bars', [])))")
if [[ "$bars_count" -lt 1 ]]; then
    echo "FAIL: history endpoint returned $bars_count bars"
    echo "Response (first 500 chars):"
    echo "$response" | head -c 500
    exit 1
fi
echo "  PASS — bars_count = $bars_count"

echo ""
echo "=== FULL smoke test PASSED ==="
SMOKE_SUCCESS=1
exit 0