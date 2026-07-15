#!/usr/bin/env bash
# =============================================================================
# DB status — exact row count for every table, in PIPELINE ORDER, so you can
# watch a cold start fill the chain stage by stage.
#
#   Usage: bash db_status.sh            # one shot
#          bash db_status.sh 30         # refresh every 30s (Ctrl-C to stop)
#
# Uses trip_gui_user (read-only). Counts are EXACT count(*), not the planner
# estimate — accurate while tables are being written, at the cost of a seq scan
# per table (a few seconds total). Pick a sane interval when watching.
# =============================================================================
set -uo pipefail

DB="${DB:-trip_ema}"
READ_ROLE="${READ_ROLE:-trip_gui_user}"
INTERVAL="${1:-}"

# Tables in the order the pipeline fills them: reference -> raw -> stock state
# -> industry index -> cross-section -> signals -> ML -> snapshot/email ->
# ledgers -> subscribers/config/schema. "section|table".
TABLES=(
  "reference|tickers"
  "reference|instrument_industry"
  "reference|instrument_shares"
  "reference|quarterly_financials"
  "reference|corporate_actions"
  "reference|fundamentals_fetch_status"
  "reference|symbol_history"
  "reference|industry_override"
  "raw+stock|ohlcv_bars"
  "raw+stock|daily_market_cap"
  "raw+stock|stock_indicator_state"
  "raw+stock|stock_indicator_enrichment"
  "industry_idx|industry_index"
  "industry_idx|industry_index_constituent"
  "industry_idx|industry_index_period"
  "industry_idx|industry_index_value"
  "industry_idx|industry_indicator_state"
  "industry_idx|industry_indicator_enrichment"
  "cross_section|sector_stock_leadlag"
  "cross_section|industry_regime_corr"
  "cross_section|industry_regime_breadth"
  "cross_section|influential_industry_ensemble"
  "cross_section|stock_benchmark_stats"
  "cross_section|industry_benchmark_stats"
  "signals|historical_signals"
  "signals|current_signals"
  "signals|intraday_signal_log"
  "signal_ml|signal_enrichment"
  "signal_ml|signal_labels"
  "signal_ml|signal_ensemble_features"
  "snapshot/email|market_overview_daily"
  "snapshot/email|notification_log"
  "ledgers|pipeline_phase_status"
  "ledgers|backfill_runs"
  "ledgers|compute_runs"
  "ledgers|etl_runs"
  "ledgers|etl_ticker_status"
  "ledgers|fundamentals_run"
  "ledgers|signal_backtest_runs"
  "ledgers|update_run_in_progress"
  "subs/config|subscribers"
  "subs/config|subscriptions"
  "subs/config|exchange_state"
  "subs/config|alembic_version"
  "subs/config|schema_migrations"
)

# Build one ordered UNION ALL so it is a single round-trip. `ord` preserves
# pipeline order; the outer SELECT hides it.
build_sql() {
  local ord=0 body=""
  for entry in "${TABLES[@]}"; do
    local section="${entry%%|*}" tbl="${entry##*|}"
    ord=$((ord + 1))
    [[ -n "$body" ]] && body+=" UNION ALL "
    body+="SELECT ${ord} AS ord, '${section}' AS section, '${tbl}' AS table_name, count(*) AS rows FROM ${tbl}"
  done
  echo "SELECT section, table_name, rows FROM ( ${body} ) q ORDER BY ord;"
}

# Any public table NOT in the curated list above — so a new table added to the
# schema later cannot silently go unmonitored.
DRIFT_SQL="
SELECT tablename AS untracked_table
  FROM pg_tables
 WHERE schemaname = 'public'
   AND tablename NOT IN ($(printf "'%s'," "${TABLES[@]##*|}" | sed 's/,$//'))
 ORDER BY tablename;"

show_once() {
  local sql; sql="$(build_sql)"
  echo "==================================================================="
  echo " DB STATUS — ${DB} — $(date '+%Y-%m-%d %H:%M:%S %Z')"
  local size; size=$(psql -U "$READ_ROLE" -d "$DB" -tAc \
    "SELECT pg_size_pretty(pg_database_size('${DB}'))" 2>/dev/null || echo "?")
  echo " database size: ${size}"
  echo "==================================================================="
  psql -U "$READ_ROLE" -d "$DB" -c "$sql"
  # flag any unmonitored table (usually prints nothing)
  local drift; drift=$(psql -U "$READ_ROLE" -d "$DB" -tAc "$DRIFT_SQL" 2>/dev/null || true)
  if [[ -n "$drift" ]]; then
    echo "  NOTE — public tables not in this script's list (add them):"
    echo "$drift" | sed 's/^/    /'
  fi
}

if [[ -z "$INTERVAL" ]]; then
  show_once
else
  if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [[ "$INTERVAL" -lt 1 ]]; then
    echo "error: interval must be a positive integer (seconds)." >&2
    exit 2
  fi
  while true; do
    clear
    show_once
    echo
    echo "(refreshing every ${INTERVAL}s — Ctrl-C to stop)"
    sleep "$INTERVAL"
  done
fi
