#!/usr/bin/env bash
# =============================================================================
# Timed cold-start backfill: OHLCV -> email. Prints start/end wall clock and a
# per-stage breakdown from the phase ledger.
#
# The EXCHANGE is a REQUIRED first argument and is carried into the CLI call and
# the phase-ledger / concurrency queries below.
#
#   Usage: bash run_coldstart_timed.sh <EXCHANGE> [extra cli args...]
#   e.g.:  bash run_coldstart_timed.sh NSE
#          bash run_coldstart_timed.sh NSE --no_fetch_bars
#
# NOTE ON --no_fetch_bars: not passed by default, so this REFETCHES every bar
# from yfinance (~2,600 tickers x ~20 years — the slowest, network-bound stage).
# Rate-limit handling is TWO mechanisms, not one:
#   PREVENTIVE — a global RequestGate (etl.yfinance_min_request_gap_seconds)
#     holds every download START at least that far apart, across ALL worker
#     threads and every retry layer. This is what stops the burst arriving.
#   REACTIVE — YFinanceSource's ThrottleGate opens a bounded exponential
#     backoff after an explicit 429 or repeated empty batches.
# Failed and IntCasting tickers are re-fetched by three SINGLE-THREADED retry
# layers at yfinance_batch_size / 2 per chunk (not one ticker at a time: that
# was itself the request storm). A sustained 429 slows the pass and, once
# max_backoffs is spent, surfaces as failed tickers, not a crash -- watch the
# [stage_1] failed count. Add --no_fetch_bars to time the COMPUTE chain only.
# =============================================================================
set -uo pipefail

EXCHANGE="${1:-}"
if [[ -z "$EXCHANGE" ]]; then
  echo "error: EXCHANGE is required." >&2
  echo "usage: bash run_coldstart_timed.sh <EXCHANGE> [extra cli args...]" >&2
  echo "  e.g. bash run_coldstart_timed.sh NSE" >&2
  exit 2
fi
shift   # remaining "$@" are extra CLI args forwarded verbatim

cd ~/PycharmProjects/WeeklyStockSignals

LOG=/tmp/coldstart_${EXCHANGE}_$(date +%Y%m%d_%H%M%S).log
START_EPOCH=$(date +%s)

{
  echo "==================================================================="
  echo " COLD-START BACKFILL -- ${EXCHANGE}"
  echo "==================================================================="
  echo " started : $(date '+%Y-%m-%d %H:%M:%S %Z')  ($(date -u '+%H:%M:%S UTC'))"
  echo " today   : $(date '+%A')"
  echo " log     : $LOG"
  echo "==================================================================="
  echo
} | tee "$LOG"

# ---- the run -------------------------------------------------------------
# NO --release-lock. That flag stomps a LIVE lock and is what let two backfills
# run concurrently last time, contaminating every stage measurement. If it
# complains about an active lock, investigate -- don't reflexively force it.
python -m trip_ema.etl.cli backfill \
    --exchange "$EXCHANGE" \
    --date_from 2006-04-01 \
    "$@" 2>&1 | tee -a "$LOG"

RC=${PIPESTATUS[0]}
END_EPOCH=$(date +%s)
ELAPSED=$((END_EPOCH - START_EPOCH))

{
  echo
  echo "==================================================================="
  echo " FINISHED -- ${EXCHANGE}"
  echo "==================================================================="
  echo " started  : $(date -d "@$START_EPOCH" '+%Y-%m-%d %H:%M:%S %Z')"
  echo " finished : $(date -d "@$END_EPOCH"   '+%Y-%m-%d %H:%M:%S %Z')"
  printf  " elapsed  : %02dh %02dm %02ds  (%d seconds)\n" \
          $((ELAPSED/3600)) $(((ELAPSED%3600)/60)) $((ELAPSED%60)) "$ELAPSED"
  echo " exit code: $RC"
  echo "==================================================================="
} | tee -a "$LOG"

# ---- per-stage breakdown -------------------------------------------------
echo
echo "=== PER-STAGE TIMING (from the phase ledger) ==="
psql -U trip_gui_user -d trip_ema -c "
SELECT phase_name,
       status,
       to_char(started_at,   'HH24:MI:SS') AS started,
       to_char(completed_at, 'HH24:MI:SS') AS ended,
       completed_at - started_at           AS duration,
       rows_affected AS row_count
  FROM pipeline_phase_status
 WHERE exchange = '${EXCHANGE}'
 ORDER BY started_at;
" | tee -a "$LOG"

# Overlap check: in a single clean run, no two stages may overlap. If any do,
# a second backfill was running concurrently and every number above is garbage.
echo
echo "=== CONCURRENCY CHECK (must print NO OVERLAP) ==="
psql -U trip_gui_user -d trip_ema -tAc "
SELECT CASE WHEN count(*) = 0
            THEN 'NO OVERLAP -- timings are trustworthy'
            ELSE 'OVERLAP DETECTED (' || count(*) || ' pairs) -- TIMINGS ARE GARBAGE, '
                 || 'two backfills ran concurrently'
       END
  FROM pipeline_phase_status a
  JOIN pipeline_phase_status b
    ON a.phase_name < b.phase_name
   AND a.exchange = b.exchange
   AND a.started_at < b.completed_at
   AND b.started_at < a.completed_at
 WHERE a.exchange = '${EXCHANGE}';
" | tee -a "$LOG"

# ---- did every stage actually produce data? ------------------------------
echo
echo "=== DID EACH STAGE PRODUCE DATA? (0 = stage fired but wrote nothing) ==="
psql -U trip_gui_user -d trip_ema -c "
SELECT 'ohlcv_bars'                AS tbl, count(*) FROM ohlcv_bars
UNION ALL SELECT 'stock_indicator_state',       count(*) FROM stock_indicator_state
UNION ALL SELECT 'stock_indicator_enrichment',  count(*) FROM stock_indicator_enrichment
UNION ALL SELECT 'daily_market_cap',            count(*) FROM daily_market_cap
UNION ALL SELECT 'industry_index_value',        count(*) FROM industry_index_value
UNION ALL SELECT 'industry_indicator_state',    count(*) FROM industry_indicator_state
UNION ALL SELECT 'industry_indicator_enrich',   count(*) FROM industry_indicator_enrichment
UNION ALL SELECT 'sector_stock_leadlag',        count(*) FROM sector_stock_leadlag
UNION ALL SELECT 'industry_regime_corr',        count(*) FROM industry_regime_corr
UNION ALL SELECT 'historical_signals',          count(*) FROM historical_signals
UNION ALL SELECT 'current_signals',             count(*) FROM current_signals
UNION ALL SELECT 'intraday_signal_log',         count(*) FROM intraday_signal_log
UNION ALL SELECT 'signal_enrichment',           count(*) FROM signal_enrichment
UNION ALL SELECT 'signal_labels',               count(*) FROM signal_labels
UNION ALL SELECT 'signal_ensemble_features',    count(*) FROM signal_ensemble_features
UNION ALL SELECT 'notification_log  <- EMAIL',  count(*) FROM notification_log
ORDER BY 1;
" | tee -a "$LOG"

echo
echo "Full log: $LOG"
