#!/usr/bin/env bash
#
# Operator health check — single-screen summary of system state.
#
# What this prints:
#   - /healthz status (service alive, cache populated)
#   - Per-exchange ticker counts (from `tickers`)
#   - Per-exchange bar counts (from `ohlcv_bars` joined to tickers)
#   - Per-exchange exchange_state row (last EoD, last listings sync,
#     initial-load completion timestamps)
#   - Alert (signal) counts: last 60 min, today, since-Monday
#   - Recent ETL run summary (last 5 runs across all exchanges)
#
# What this does NOT do:
#   - Run any ETL
#   - Modify any data
#   - Touch the GUI
#
# Read-only against the service + database. Safe to run any time.
#
# Usage:
#   ./scripts/health_check.sh
#   ./scripts/health_check.sh --no-service     # skip the HTTP probe
#                                              # (use if service is down
#                                              # but you still want DB
#                                              # state)
#
# Prereqs:
#   - DB password resolved in this order:
#       1. PGPASSWORD env var (if already set in the shell)
#       2. config.yaml's `database.password_env` env var (auto-
#          resolved by this script; usually $TRIP_EMA_DB_PASSWORD).
#       3. ~/.pgpass fallback (legacy).
#   - trip-ema service running on localhost:8000 (unless --no-service).
#   - psql and curl on PATH.

set -uo pipefail   # NOT -e: we want partial successes to still print
                   # the sections that did work.

# ===== Configuration =====
SERVICE_PORT=8000
PG_HOST="localhost"
PG_USER="trip_ema_app"
PG_DB="trip_ema"
HEALTH_URL="http://localhost:${SERVICE_PORT}/readyz"
# =========================

SKIP_SERVICE=0
if [[ "${1:-}" == "--no-service" ]]; then
    SKIP_SERVICE=1
fi

# Resolve the DB password from the same source the service uses.
# config.yaml has `password_env: TRIP_EMA_DB_PASSWORD`; read that
# attribute, then export PGPASSWORD from the env var it names if set.
# Falls through silently to ~/.pgpass / interactive prompt otherwise.
if [[ -z "${PGPASSWORD:-}" ]]; then
    PASSWORD_ENV=$(.venv/bin/python -c "
from trip_ema.config.loader import load_config
cfg = load_config('config.yaml')
print(getattr(cfg.database, 'password_env', '') or '')
" 2>/dev/null)
    if [[ -n "$PASSWORD_ENV" && -n "${!PASSWORD_ENV:-}" ]]; then
        export PGPASSWORD="${!PASSWORD_ENV}"
    fi
fi

PSQL=(psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" -X -q -A)

# Helper: run a single SQL and print result. -X skips ~/.psqlrc.
# -q suppresses "Time:" etc. -A flips to unaligned output so the
# output is closer to CSV (easier to read in narrow terminals).
psql_query() {
    local sql="$1"
    "${PSQL[@]}" -c "$sql" 2>&1
}

# ---------- Section 1: /healthz ---------- #
echo "============================================================="
echo "  trip-ema health check  ($(date '+%Y-%m-%d %H:%M:%S %Z'))"
echo "============================================================="
echo ""
if [[ $SKIP_SERVICE -eq 1 ]]; then
    echo "[1/6] Service /readyz: SKIPPED (--no-service)"
else
    echo "[1/6] Service /readyz"
    # /readyz returns 200 (ready) or 503 (not_ready with issues).
    # We want the body either way, so drop -f.
    if response=$(curl -sS --max-time 5 "$HEALTH_URL" 2>&1); then
        echo "$response" | python3 -m json.tool 2>/dev/null \
            | sed 's/^/  /' \
            || echo "  $response"
    else
        echo "  FAIL: service not reachable at $HEALTH_URL"
        echo "  (start with ./scripts/restart_service.sh)"
    fi
fi
echo ""

# ---------- Section 2: per-exchange ticker counts ---------- #
echo "[2/6] Tickers per exchange (active = is_active TRUE)"
psql_query "
SELECT
    exchange,
    COUNT(*) FILTER (WHERE is_active)        AS active,
    COUNT(*) FILTER (WHERE NOT is_active)    AS inactive,
    COUNT(*)                                  AS total
FROM tickers
GROUP BY exchange
ORDER BY exchange;
" | sed 's/^/  /'
echo ""

# ---------- Section 3: per-exchange bar counts ---------- #
echo "[3/6] OHLCV bars per exchange (all timeframes)"
psql_query "
SELECT
    t.exchange,
    b.timeframe,
    COUNT(*)                          AS bars,
    MIN(b.bar_timestamp)::date        AS first_bar,
    MAX(b.bar_timestamp)::date        AS last_bar
FROM ohlcv_bars b
JOIN tickers t USING (ticker_id)
GROUP BY t.exchange, b.timeframe
ORDER BY t.exchange, b.timeframe;
" | sed 's/^/  /'
echo ""

# ---------- Section 4: exchange_state (last EoD, listings, init load) ---------- #
echo "[4/6] Exchange state (per-exchange ETL snapshot, from exchange_state)"
psql_query "
SELECT
    exchange,
    initial_load_started_at::date           AS init_start,
    initial_load_completed_at::date         AS init_done,
    last_eod_run_at::date                   AS last_eod,
    last_eod_outcome                        AS last_eod_outcome,
    last_eod_data_date                      AS data_date,
    last_listings_sync_at::date             AS last_listings,
    last_intraday_run_at::date              AS last_intraday,
    last_tier_recompute_at::date            AS last_tiering
FROM exchange_state
ORDER BY exchange;
" | sed 's/^/  /'
echo ""

# ---------- Section 5: alert / signal_events counts ---------- #
echo "[5/6] Alerts (signal_events) per exchange"
echo "      Rolling windows: last 60min, today (since 00:00), week-to-date (since Monday)"
psql_query "
WITH bounds AS (
    SELECT
        NOW() - INTERVAL '60 minutes'                                         AS h_start,
        DATE_TRUNC('day',  NOW())                                             AS d_start,
        DATE_TRUNC('week', NOW())                                             AS w_start
)
SELECT
    t.exchange,
    COUNT(*) FILTER (WHERE s.computed_at >= b.h_start)                AS last_60min,
    COUNT(*) FILTER (WHERE s.computed_at >= b.d_start)                AS today,
    COUNT(*) FILTER (WHERE s.computed_at >= b.w_start)                AS this_week,
    COUNT(*) FILTER (WHERE s.signal_type = 'BUY'  AND s.computed_at >= b.w_start) AS week_buys,
    COUNT(*) FILTER (WHERE s.signal_type = 'SELL' AND s.computed_at >= b.w_start) AS week_sells
FROM historical_signals s
JOIN tickers t USING (ticker_id)
CROSS JOIN bounds b
GROUP BY t.exchange
ORDER BY t.exchange;
" | sed 's/^/  /'
echo ""
echo "  (DATE_TRUNC('week', ...) starts Monday in Postgres — see"
echo "   https://www.postgresql.org/docs/current/functions-datetime.html )"
echo ""

# ---------- Section 6: recent ETL runs ---------- #
echo "[6/6] Recent ETL runs (last 5 across all exchanges)"
psql_query "
SELECT
    run_id,
    kind,
    outcome,
    to_char(started_at, 'YYYY-MM-DD HH24:MI')   AS started,
    CASE
        WHEN finished_at IS NULL THEN '(in flight)'
        ELSE LPAD((EXTRACT(EPOCH FROM (finished_at - started_at))::int)::text, 4) || 's'
    END                                          AS dur,
    tickers_succeeded                           AS ok,
    tickers_failed                              AS fail,
    bars_written                                AS bars
FROM etl_runs
ORDER BY started_at DESC
LIMIT 5;
" | sed 's/^/  /'
echo ""

echo "============================================================="
echo "Done."
echo "============================================================="
