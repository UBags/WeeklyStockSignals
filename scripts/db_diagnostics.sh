#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# db_diagnostics.sh
#
# Read-only diagnostic snapshot of the trip_ema database.
# Prerequisites: setup_psql_access.sh has been run (so `psql -U trip_etl_user
# -d trip_ema` works without a password prompt via ~/.pgpass).
#
# Sections:
#   (a) bars-per-day for the last 3 days and today, per exchange in config.yaml
#   (b) tickers missing bars on each of those 4 days, per exchange
#   (c) signals generated and their timestamps for the last 2 days
#   (d) last 2 EoD runs per exchange
#   (e) last price-fetch timestamps per exchange (EoD + intraday)
#   (f) last ticker-list refresh per exchange (from exchange_state)
#
# Usage:    ./db_diagnostics.sh [path/to/config.yaml]
# Default:  ./config.yaml in cwd
# -----------------------------------------------------------------------------

set -euo pipefail

CONFIG_FILE="${1:-config.yaml}"
# trip_gui_user has SELECT on all tables (per Phase 12 role grants), so it's
# the right identity for diagnostics. trip_etl_user can't read signal_events.
DB_USER="${DB_USER:-trip_gui_user}"
DB_NAME="${DB_NAME:-trip_ema}"

# Dates render via the IST clock (Bangalore-local, matches how the operator
# thinks about NSE/BSE bar dates). Override via TZ_DISPLAY if needed.
TZ_DISPLAY="${TZ_DISPLAY:-Asia/Kolkata}"

# ----- Preflight --------------------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
    echo "ERROR: psql not found in PATH" >&2; exit 1
fi
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config.yaml not found at: $CONFIG_FILE" >&2
    echo "       pass path as first arg: $0 /path/to/config.yaml" >&2
    exit 1
fi

# Extract exchanges from market_hours: block.
# Grabs every `- exchange: NSE` line (handles both `NSE` and `"NSE"`).
EXCHANGES=( $(grep -E '^\s*-\s*exchange:\s*' "$CONFIG_FILE" \
              | sed -E 's/^\s*-\s*exchange:\s*"?([A-Z0-9_-]+)"?\s*$/\1/' \
              | awk '!seen[$0]++') )

if [[ ${#EXCHANGES[@]} -eq 0 ]]; then
    echo "ERROR: no exchanges found under market_hours: in $CONFIG_FILE" >&2
    exit 1
fi

# Comma-separated SQL list, e.g. 'NSE','BSE','NYSE'
EX_SQL_LIST="$(printf "'%s'," "${EXCHANGES[@]}" | sed 's/,$//')"

# Connection smoke test (so we fail fast with a clear error).
if ! psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT 1;" >/dev/null 2>&1; then
    echo "ERROR: cannot connect as $DB_USER to $DB_NAME." >&2
    echo "       run scripts/setup_psql_access.sh first." >&2
    exit 1
fi

# Section header helper
hdr() {
    echo
    echo "=============================================================="
    echo "$1"
    echo "=============================================================="
}

# Run a SQL block, isolating each section's error (so one failure doesn't
# halt the rest of the report).
runsql() {
    psql -U "$DB_USER" -d "$DB_NAME" -P pager=off -X \
         -v ON_ERROR_STOP=0 -c "$1" || echo "[!] section query failed"
}

echo "Diagnostics for database '$DB_NAME' (user $DB_USER)"
echo "Exchanges from $CONFIG_FILE : ${EXCHANGES[*]}"
echo "Display timezone           : $TZ_DISPLAY"
echo "Snapshot taken at          : $(date -Iseconds)"

# ============================================================================
# (a) Bars-per-day for today + last 3 days, per exchange
# ============================================================================
hdr "(a) Bars per day per exchange — today + last 3 days (1D timeframe)"

runsql "
WITH days AS (
    SELECT (CURRENT_DATE AT TIME ZONE '$TZ_DISPLAY')::date - d AS bar_date
    FROM generate_series(0, 3) d
),
ex AS (
    SELECT exchange FROM (VALUES $(
        first=1
        for ex in "${EXCHANGES[@]}"; do
            if [[ $first -eq 1 ]]; then printf "('%s')" "$ex"; first=0
            else printf ",('%s')" "$ex"; fi
        done
    )) v(exchange)
)
SELECT
    e.exchange,
    d.bar_date,
    COALESCE(COUNT(b.ticker_id), 0)              AS bar_count,
    COALESCE(t_count.n, 0)                       AS universe_size
FROM ex e
CROSS JOIN days d
LEFT JOIN tickers t
       ON t.exchange = e.exchange
LEFT JOIN ohlcv_bars b
       ON b.ticker_id = t.ticker_id
      AND b.timeframe = '1D'
      AND (b.bar_timestamp AT TIME ZONE '$TZ_DISPLAY')::date = d.bar_date
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n FROM tickers WHERE exchange = e.exchange AND is_active
) t_count ON TRUE
GROUP BY e.exchange, d.bar_date, t_count.n
ORDER BY e.exchange, d.bar_date DESC;
"

# Highlight exchanges that have ZERO tickers in the universe — useful since
# 'bar_count=0' on its own looks identical for both 'nothing fetched' and
# 'nothing to fetch'.
echo
echo "Exchanges in config.yaml with ZERO tickers in DB (no universe — scheduler ticks are no-ops):"
runsql "
SELECT v.exchange,
       CASE WHEN tc.n IS NULL OR tc.n = 0 THEN 'EMPTY — run refresh-listings'
            ELSE tc.n::text || ' active tickers'
       END AS status
FROM (VALUES $(
    first=1
    for ex in "${EXCHANGES[@]}"; do
        if [[ $first -eq 1 ]]; then printf "('%s')" "$ex"; first=0
        else printf ",('%s')" "$ex"; fi
    done
)) v(exchange)
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n FROM tickers WHERE exchange = v.exchange AND is_active
) tc ON TRUE
ORDER BY v.exchange;
"

# ============================================================================
# (b) Which tickers are MISSING bars per day
# ============================================================================
hdr "(b) Tickers missing 1D bars per exchange per day — today + last 3 days"
echo "     Per (exchange, date): total count + up to 30 sample symbols"

for EX in "${EXCHANGES[@]}"; do
    echo
    echo "--- $EX -----------------------------------------------------"
    runsql "
WITH days AS (
    SELECT (CURRENT_DATE AT TIME ZONE '$TZ_DISPLAY')::date - d AS bar_date
    FROM generate_series(0, 3) d
),
active_tickers AS (
    SELECT ticker_id, symbol
    FROM tickers
    WHERE exchange = '$EX' AND is_active = TRUE
),
missing AS (
    SELECT d.bar_date, at.symbol
    FROM days d
    CROSS JOIN active_tickers at
    WHERE NOT EXISTS (
        SELECT 1 FROM ohlcv_bars b
        WHERE b.ticker_id = at.ticker_id
          AND b.timeframe = '1D'
          AND (b.bar_timestamp AT TIME ZONE '$TZ_DISPLAY')::date = d.bar_date
    )
)
SELECT
    bar_date,
    COUNT(*)                            AS missing_total,
    LEFT(STRING_AGG(symbol, ', ' ORDER BY symbol), 800) ||
        CASE WHEN COUNT(*) > 30 THEN ' …(+' || (COUNT(*) - 30)::text || ' more)' ELSE '' END
                                        AS sample_symbols
FROM (
    SELECT bar_date, symbol,
           ROW_NUMBER() OVER (PARTITION BY bar_date ORDER BY symbol) AS rn
    FROM missing
) sub
WHERE rn <= 30
GROUP BY bar_date
UNION ALL
SELECT
    d.bar_date,
    0 AS missing_total,
    '(none missing)'::text AS sample_symbols
FROM days d
WHERE NOT EXISTS (SELECT 1 FROM missing m WHERE m.bar_date = d.bar_date)
ORDER BY 1 DESC;
"
done

# ============================================================================
# (c) Signals generated in the last 2 days
# ============================================================================
hdr "(c) Signals generated — last 2 days (live signal_events log)"

runsql "
SELECT
    t.exchange,
    t.symbol,
    se.timeframe,
    se.signal_type,
    (se.bar_timestamp     AT TIME ZONE '$TZ_DISPLAY') AS bar_local,
    (se.computed_at       AT TIME ZONE '$TZ_DISPLAY') AS computed_local,
    se.price_at_signal
FROM signal_events se
JOIN tickers t ON t.ticker_id = se.ticker_id
WHERE t.exchange IN ($EX_SQL_LIST)
  AND se.computed_at >= NOW() - INTERVAL '2 days'
ORDER BY
    (se.computed_at AT TIME ZONE '$TZ_DISPLAY')::date DESC,
    se.signal_type ASC,
    t.symbol ASC;
"

echo
echo "Signal count summary (last 2 days):"
runsql "
SELECT
    t.exchange,
    se.signal_type,
    COUNT(*) AS n
FROM signal_events se
JOIN tickers t ON t.ticker_id = se.ticker_id
WHERE t.exchange IN ($EX_SQL_LIST)
  AND se.computed_at >= NOW() - INTERVAL '2 days'
GROUP BY t.exchange, se.signal_type
ORDER BY t.exchange, se.signal_type;
"

# ============================================================================
# (d) Last 2 EoD runs per exchange
# ============================================================================
hdr "(d) Last 2 EoD runs per exchange (from etl_runs)"

runsql "
WITH ranked AS (
    SELECT
        exchange,
        kind,
        (started_at  AT TIME ZONE '$TZ_DISPLAY') AS started_local,
        (finished_at AT TIME ZONE '$TZ_DISPLAY') AS finished_local,
        outcome,
        tickers_attempted,
        tickers_succeeded,
        tickers_failed,
        bars_written,
        ROW_NUMBER() OVER (PARTITION BY exchange ORDER BY started_at DESC) AS rn
    FROM etl_runs
    WHERE kind = 'eod'
      AND exchange IN ($EX_SQL_LIST)
)
SELECT exchange, started_local, finished_local, outcome,
       tickers_attempted, tickers_succeeded, tickers_failed, bars_written
FROM ranked
WHERE rn <= 2
ORDER BY exchange, started_local DESC;
"

# ============================================================================
# (e) Last price-fetch timestamps per exchange
# ============================================================================
hdr "(e) Last price-fetch timestamps per exchange (EoD + intraday)"

# Prefer exchange_state for the authoritative \"last X happened\" snapshot;
# augment with most-recent etl_runs rows so we can show last-fetch even when
# exchange_state wasn't populated by an older deployment.
runsql "
SELECT
    es.exchange,
    (es.last_eod_run_at      AT TIME ZONE '$TZ_DISPLAY') AS last_eod_run_local,
    es.last_eod_outcome,
    es.last_eod_data_date,
    (es.last_intraday_run_at AT TIME ZONE '$TZ_DISPLAY') AS last_intraday_local
FROM exchange_state es
WHERE es.exchange IN ($EX_SQL_LIST)
ORDER BY es.exchange;
"

echo
echo "Fallback: latest etl_runs row per (exchange, kind) for kinds = eod / intraday / backfill"
runsql "
WITH ranked AS (
    SELECT
        exchange,
        kind,
        (started_at  AT TIME ZONE '$TZ_DISPLAY') AS started_local,
        (finished_at AT TIME ZONE '$TZ_DISPLAY') AS finished_local,
        outcome,
        bars_written,
        ROW_NUMBER() OVER (
            PARTITION BY exchange, kind
            ORDER BY started_at DESC
        ) AS rn
    FROM etl_runs
    WHERE exchange IN ($EX_SQL_LIST)
      AND kind IN ('eod', 'intraday', 'backfill')
)
SELECT exchange, kind, started_local, finished_local, outcome, bars_written
FROM ranked
WHERE rn = 1
ORDER BY exchange, kind;
"

# ============================================================================
# (f) Last ticker-list refresh per exchange
# ============================================================================
hdr "(f) Last ticker-list refresh per exchange (listings sync)"

runsql "
SELECT
    es.exchange,
    (es.last_listings_sync_at AT TIME ZONE '$TZ_DISPLAY') AS last_listings_sync_local,
    es.last_listings_run_id
FROM exchange_state es
WHERE es.exchange IN ($EX_SQL_LIST)
ORDER BY es.exchange;
"

echo
echo "Fallback: latest etl_runs row per exchange with kind='listings'"
runsql "
WITH ranked AS (
    SELECT
        exchange,
        (started_at  AT TIME ZONE '$TZ_DISPLAY') AS started_local,
        (finished_at AT TIME ZONE '$TZ_DISPLAY') AS finished_local,
        outcome,
        ROW_NUMBER() OVER (PARTITION BY exchange ORDER BY started_at DESC) AS rn
    FROM etl_runs
    WHERE kind = 'listings'
      AND exchange IN ($EX_SQL_LIST)
)
SELECT exchange, started_local, finished_local, outcome
FROM ranked
WHERE rn = 1
ORDER BY exchange;
"

echo
echo "Per-exchange ticker counts in the tickers table (sanity):"
runsql "
SELECT exchange,
       COUNT(*) FILTER (WHERE is_active)                  AS active,
       COUNT(*) FILTER (WHERE NOT is_active)              AS inactive,
       COUNT(*)                                           AS total,
       MAX(created_at AT TIME ZONE '$TZ_DISPLAY')         AS newest_row_local
FROM tickers
WHERE exchange IN ($EX_SQL_LIST)
GROUP BY exchange
ORDER BY exchange;
"

echo
echo "=============================================================="
echo "Diagnostics complete."
echo "=============================================================="
