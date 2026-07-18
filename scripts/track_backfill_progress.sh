#!/usr/bin/env bash
# =============================================================================
# track_backfill_progress.sh — read-only progress view of a backfill / cold start
#
# Answers, in order: which run is live, which phase is it on, how far has each
# derived table filled, and WHAT IS STILL MISSING (the gap tables — these are
# what a rate-limited sweep leaves behind).
#
# READ ONLY. Every query runs as trip_gui_user. Nothing here writes.
#
# Usage:
#   bash scripts/track_backfill_progress.sh              # one snapshot, NSE
#   bash scripts/track_backfill_progress.sh BSE          # another exchange
#   bash scripts/track_backfill_progress.sh NSE --watch  # refresh every 30s
#   bash scripts/track_backfill_progress.sh NSE --gaps   # only the gap report
#   bash scripts/track_backfill_progress.sh NSE --ohlcv  # bar counts + thin tickers
#   THIN_PCT=30 bash scripts/track_backfill_progress.sh NSE --ohlcv
#   WATCH_SECS=10 bash scripts/track_backfill_progress.sh NSE --watch
# =============================================================================
set -uo pipefail

DB="${DB:-trip_ema}"
ROLE="${READ_ROLE:-trip_gui_user}"
EXCHANGE="${1:-NSE}"
MODE="${2:-}"
WATCH_SECS="${WATCH_SECS:-30}"
THIN_PCT="${THIN_PCT:-50}"   # "thin" = fewer bars than this %% of the average

q()  { psql -U "$ROLE" -d "$DB" -X -q -c "$1" 2>&1; }
qt() { psql -U "$ROLE" -d "$DB" -X -q -tAc "$1" 2>/dev/null; }

hdr() {
  echo
  echo "==================================================================="
  echo " $1"
  echo "==================================================================="
}

# ---------------------------------------------------------------------------
section_runs() {
  hdr "BACKFILL RUNS (most recent 5)"
  q "
  SELECT run_id, exchange,
         to_char(started_at  AT TIME ZONE 'Asia/Kolkata','DD Mon HH24:MI') AS started_ist,
         COALESCE(to_char(finished_at AT TIME ZONE 'Asia/Kolkata','DD Mon HH24:MI'),
                  '-- RUNNING --')                                        AS finished_ist,
         CASE WHEN finished_at IS NULL
              THEN to_char(now() - started_at,  'HH24:MI:SS')
              ELSE to_char(finished_at - started_at,'HH24:MI:SS')
         END AS elapsed,
         date_from, date_to, refetch_bars, pid,
         left(ticker_scope, 18) AS scope
    FROM backfill_runs
   WHERE exchange = '${EXCHANGE}'
   ORDER BY run_id DESC
   LIMIT 5;"
}

section_phases() {
  hdr "PIPELINE PHASES — ${EXCHANGE} (latest run)"
  q "
  WITH latest AS (
      SELECT max(run_id) AS run_id FROM backfill_runs WHERE exchange='${EXCHANGE}'
  )
  SELECT p.phase_name, p.status, p.rows_affected,
         to_char(p.started_at   AT TIME ZONE 'Asia/Kolkata','HH24:MI:SS') AS started,
         CASE WHEN p.completed_at IS NULL
              THEN to_char(now() - p.started_at,'HH24:MI:SS') || ' (running)'
              ELSE to_char(p.completed_at - p.started_at,'HH24:MI:SS')
         END AS took,
         left(COALESCE(p.last_error,''), 40) AS last_error
    FROM pipeline_phase_status p, latest l
   WHERE p.exchange = '${EXCHANGE}'
     AND (p.backfill_run_id = l.run_id OR p.backfill_run_id IS NULL)
   ORDER BY COALESCE(p.started_at, p.updated_at);"
}

section_fundamentals() {
  hdr "FUNDAMENTALS SWEEPS (industry / shares / financials)"
  # PARTIAL here == some tickers were rate-limited. succeeded/failed/skipped
  # are the sweep's own counters.
  q "
  SELECT run_id, phase, status, succeeded, failed, skipped,
         to_char(started_at AT TIME ZONE 'Asia/Kolkata','DD Mon HH24:MI') AS started_ist,
         CASE WHEN completed_at IS NULL
              THEN to_char(now() - started_at,'HH24:MI:SS') || ' (running)'
              ELSE to_char(completed_at - started_at,'HH24:MI:SS')
         END AS took
    FROM fundamentals_run
   WHERE exchange = '${EXCHANGE}'
   ORDER BY run_id DESC
   LIMIT 9;"
}

section_coverage() {
  hdr "COVERAGE — ${EXCHANGE} (derived tables vs active tickers)"
  q "
  WITH t AS (
      SELECT count(*)::numeric AS n
        FROM tickers WHERE exchange='${EXCHANGE}' AND is_active
  )
  SELECT label, covered, t.n::bigint AS active_tickers,
         to_char(100.0*covered/NULLIF(t.n,0),'990.9') || ' %' AS pct
    FROM t, (
      SELECT 'instrument_industry'   AS label, count(DISTINCT ii.ticker_id) AS covered
        FROM instrument_industry ii JOIN tickers k USING (ticker_id)
       WHERE k.exchange='${EXCHANGE}' AND k.is_active
      UNION ALL
      SELECT 'instrument_shares', count(DISTINCT s.ticker_id)
        FROM instrument_shares s JOIN tickers k USING (ticker_id)
       WHERE k.exchange='${EXCHANGE}' AND k.is_active
      UNION ALL
      SELECT 'quarterly_financials', count(DISTINCT f.ticker_id)
        FROM quarterly_financials f JOIN tickers k USING (ticker_id)
       WHERE k.exchange='${EXCHANGE}' AND k.is_active
      UNION ALL
      SELECT 'ohlcv_bars (1D)', count(DISTINCT b.ticker_id)
        FROM ohlcv_bars b JOIN tickers k USING (ticker_id)
       WHERE k.exchange='${EXCHANGE}' AND k.is_active AND b.timeframe='1D'
      UNION ALL
      SELECT 'ohlcv_bars (1W)', count(DISTINCT b.ticker_id)
        FROM ohlcv_bars b JOIN tickers k USING (ticker_id)
       WHERE k.exchange='${EXCHANGE}' AND k.is_active AND b.timeframe='1W'
      UNION ALL
      SELECT 'daily_market_cap', count(DISTINCT m.ticker_id)
        FROM daily_market_cap m JOIN tickers k USING (ticker_id)
       WHERE k.exchange='${EXCHANGE}' AND k.is_active
      UNION ALL
      SELECT 'stock_indicator_state', count(DISTINCT x.ticker_id)
        FROM stock_indicator_state x JOIN tickers k USING (ticker_id)
       WHERE k.exchange='${EXCHANGE}' AND k.is_active
    ) c
   ORDER BY label;"
}

section_ohlcv() {
  hdr "OHLCV VOLUME — ${EXCHANGE}"
  q "
  SELECT
    (SELECT count(*) FROM tickers
      WHERE exchange='${EXCHANGE}')                              AS tickers_total,
    (SELECT count(*) FROM tickers
      WHERE exchange='${EXCHANGE}' AND is_active)                AS tickers_active,
    (SELECT count(*) FROM tickers
      WHERE exchange='${EXCHANGE}' AND delisted_at IS NOT NULL)  AS tickers_delisted,
    (SELECT count(*) FROM ohlcv_bars b JOIN tickers k USING (ticker_id)
      WHERE k.exchange='${EXCHANGE}')                            AS bars_total,
    (SELECT count(*) FROM ohlcv_bars b JOIN tickers k USING (ticker_id)
      WHERE k.exchange='${EXCHANGE}' AND b.timeframe='1D')       AS bars_1d,
    (SELECT count(*) FROM ohlcv_bars b JOIN tickers k USING (ticker_id)
      WHERE k.exchange='${EXCHANGE}' AND b.timeframe='1W')       AS bars_1w;"

  echo "-- per-ticker 1D bar depth (active tickers with at least one bar) --"
  q "
  WITH per AS (
      SELECT k.ticker_id, count(b.*) AS bars
        FROM tickers k
        LEFT JOIN ohlcv_bars b
               ON b.ticker_id = k.ticker_id AND b.timeframe = '1D'
       WHERE k.exchange='${EXCHANGE}' AND k.is_active
       GROUP BY k.ticker_id
  )
  SELECT count(*) FILTER (WHERE bars > 0)                    AS tickers_with_bars,
         count(*) FILTER (WHERE bars = 0)                    AS tickers_no_data,
         round(avg(bars) FILTER (WHERE bars > 0))            AS avg_bars,
         round(percentile_cont(0.5) WITHIN GROUP (ORDER BY bars)
               FILTER (WHERE bars > 0))                      AS median_bars,
         min(bars) FILTER (WHERE bars > 0)                   AS min_bars,
         max(bars)                                           AS max_bars
    FROM per;"
}

section_thin() {
  hdr "TICKERS WITH NO DATA — ${EXCHANGE}"
  echo "Active tickers with ZERO 1D bars. A fetch never landed for these."
  q "
  SELECT k.ticker_id, k.symbol, k.instrument_type,
         to_char(k.listing_date,'YYYY-MM-DD') AS listed
    FROM tickers k
    LEFT JOIN ohlcv_bars b
           ON b.ticker_id = k.ticker_id AND b.timeframe='1D'
   WHERE k.exchange='${EXCHANGE}' AND k.is_active AND b.ticker_id IS NULL
   ORDER BY k.symbol
   LIMIT 40;"

  hdr "THIN TICKERS — ${EXCHANGE}  (< ${THIN_PCT}% of average 1D bars)"
  echo "Fewer bars than peers. Check 'listed' before alarm: a 2025 IPO SHOULD be"
  echo "thin. A ticker listed long ago that is thin means a truncated fetch."
  q "
  WITH per AS (
      SELECT k.ticker_id, k.symbol, k.listing_date,
             count(b.*)                                   AS bars,
             min(b.bar_timestamp) AT TIME ZONE 'Asia/Kolkata' AS first_bar,
             max(b.bar_timestamp) AT TIME ZONE 'Asia/Kolkata' AS last_bar
        FROM tickers k
        JOIN ohlcv_bars b
          ON b.ticker_id = k.ticker_id AND b.timeframe='1D'
       WHERE k.exchange='${EXCHANGE}' AND k.is_active
       GROUP BY k.ticker_id, k.symbol, k.listing_date
  ), stats AS (
      SELECT avg(bars) AS avg_bars FROM per
  )
  SELECT p.ticker_id, p.symbol, p.bars,
         round(100.0 * p.bars / NULLIF(s.avg_bars,0)) || ' %' AS pct_of_avg,
         to_char(p.listing_date,'YYYY-MM-DD')      AS listed,
         to_char(p.first_bar,'YYYY-MM-DD')         AS first_bar,
         to_char(p.last_bar,'YYYY-MM-DD')          AS last_bar,
         CASE WHEN p.listing_date IS NOT NULL
                   AND p.first_bar::date - p.listing_date <= 30
              THEN 'ok: new listing'
              ELSE 'CHECK: truncated?'
         END AS verdict
    FROM per p, stats s
   WHERE p.bars < s.avg_bars * (${THIN_PCT} / 100.0)
   ORDER BY p.bars
   LIMIT 30;"
}

section_signals() {
  hdr "SIGNALS / ML TABLES — ${EXCHANGE}"
  q "
  SELECT 'historical_signals'  AS table_name, count(*) AS rows,
         to_char(max(h.signal_date),'YYYY-MM-DD') AS latest
    FROM historical_signals h JOIN tickers k USING (ticker_id)
   WHERE k.exchange='${EXCHANGE}'
  UNION ALL
  SELECT 'current_signals', count(*),
         -- bar_timestamp is the IST-Monday WEEK ANCHOR, not a trading day
         to_char(max(c.bar_timestamp) AT TIME ZONE 'Asia/Kolkata','YYYY-MM-DD')
    FROM current_signals c JOIN tickers k USING (ticker_id)
   WHERE k.exchange='${EXCHANGE}'
  UNION ALL
  SELECT 'signal_enrichment', count(*), to_char(max(e.signal_date),'YYYY-MM-DD')
    FROM signal_enrichment e JOIN tickers k USING (ticker_id)
   WHERE k.exchange='${EXCHANGE}'
  UNION ALL
  SELECT 'signal_labels (all)', count(*), NULL
    FROM signal_labels
  UNION ALL
  SELECT 'signal_labels (forward-walk)', count(*) FILTER (WHERE forward_walk_done), NULL
    FROM signal_labels
  UNION ALL
  SELECT 'signal_labels (censored)', count(*) FILTER (WHERE window_censored), NULL
    FROM signal_labels
  UNION ALL
  SELECT 'industry_index_value', count(*), to_char(max(trade_date),'YYYY-MM-DD')
    FROM industry_index_value
  UNION ALL
  SELECT 'notification_log', count(*),
         to_char(max(last_sent_at) AT TIME ZONE 'Asia/Kolkata','YYYY-MM-DD HH24:MI')
    FROM notification_log;"
}

section_gaps() {
  hdr "GAPS — ${EXCHANGE}  (what a rate-limited sweep left behind)"
  echo "Tickers with NO row in the reference tables. These are re-fetched by the"
  echo "next gap-only sweep; a 429 does NOT mark them UNAVAILABLE."
  q "
  SELECT 'industry'   AS dataset, count(*) AS missing_tickers
    FROM tickers k LEFT JOIN instrument_industry i USING (ticker_id)
   WHERE k.exchange='${EXCHANGE}' AND k.is_active AND i.ticker_id IS NULL
  UNION ALL
  SELECT 'shares', count(*)
    FROM tickers k LEFT JOIN instrument_shares s USING (ticker_id)
   WHERE k.exchange='${EXCHANGE}' AND k.is_active AND s.ticker_id IS NULL
  UNION ALL
  SELECT 'financials', count(*)
    FROM tickers k LEFT JOIN quarterly_financials f USING (ticker_id)
   WHERE k.exchange='${EXCHANGE}' AND k.is_active AND f.ticker_id IS NULL
  UNION ALL
  SELECT 'ohlcv_bars(1D)', count(*)
    FROM tickers k LEFT JOIN ohlcv_bars b
      ON b.ticker_id=k.ticker_id AND b.timeframe='1D'
   WHERE k.exchange='${EXCHANGE}' AND k.is_active AND b.ticker_id IS NULL
   ORDER BY 1;"

  echo
  echo "-- Marked UNAVAILABLE upstream (asked and answered: NOT a rate limit,"
  echo "   these will NOT be retried by a gap-only sweep) --"
  q "
  SELECT dataset, count(*) AS tickers, max(attempts) AS max_attempts
    FROM fundamentals_fetch_status
   GROUP BY dataset ORDER BY dataset;"

  echo
  echo "-- First 25 tickers still missing industry (the rate-limited cohort) --"
  q "
  SELECT k.ticker_id, k.symbol
    FROM tickers k
    LEFT JOIN instrument_industry i USING (ticker_id)
    LEFT JOIN fundamentals_fetch_status fs
           ON fs.ticker_id = k.ticker_id AND fs.dataset='industry'
   WHERE k.exchange='${EXCHANGE}' AND k.is_active
     AND i.ticker_id IS NULL
     AND fs.ticker_id IS NULL          -- exclude the genuinely UNAVAILABLE
   ORDER BY k.symbol
   LIMIT 25;"
}

section_hint() {
  local miss
  miss=$(qt "
    SELECT count(*) FROM tickers k
      LEFT JOIN instrument_industry i USING (ticker_id)
      LEFT JOIN fundamentals_fetch_status fs
             ON fs.ticker_id=k.ticker_id AND fs.dataset='industry'
     WHERE k.exchange='${EXCHANGE}' AND k.is_active
       AND i.ticker_id IS NULL AND fs.ticker_id IS NULL")
  if [[ "${miss:-0}" -gt 0 ]]; then
    hdr "NEXT ACTION"
    echo "  ${miss} ticker(s) still missing industry after rate-limiting."
    echo "  Wait for Yahoo's window to clear (15+ min), then fill the gaps:"
    echo
    echo "    python -m trip_ema.etl.cli refresh-industry \\"
    echo "        --exchange ${EXCHANGE} --only-missing --force"
    echo
    echo "  Or resume the PARTIAL run in place (re-fetches only what it missed):"
    local rid
    rid=$(qt "SELECT run_id FROM fundamentals_run
               WHERE exchange='${EXCHANGE}' AND phase='industry'
                 AND status IN ('PARTIAL','RUNNING')
               ORDER BY run_id DESC LIMIT 1")
    [[ -n "${rid:-}" ]] && \
      echo "    python -m trip_ema.etl.cli refresh-industry --exchange ${EXCHANGE} --resume ${rid}"
    echo
    echo "  Lower the request rate first if 429s persist (config.yaml, etl:):"
    echo "    fundamentals_concurrency: 2"
    echo "    fundamentals_request_delay_seconds: 1.5"
  fi
}

snapshot() {
  echo "==================================================================="
  echo " BACKFILL PROGRESS — ${EXCHANGE}   $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo " db=${DB} role=${ROLE}"
  echo "==================================================================="
  if [[ "$MODE" == "--gaps" ]]; then
    section_gaps; section_hint; return
  fi
  if [[ "$MODE" == "--ohlcv" ]]; then
    section_ohlcv; section_thin; return
  fi
  section_runs
  section_phases
  section_fundamentals
  section_coverage
  section_ohlcv
  section_thin
  section_signals
  section_gaps
  section_hint
}

if [[ "$MODE" == "--watch" ]]; then
  while true; do
    clear
    snapshot
    echo
    echo "-- refreshing every ${WATCH_SECS}s (Ctrl-C to stop) --"
    sleep "$WATCH_SECS"
  done
else
  snapshot
fi
