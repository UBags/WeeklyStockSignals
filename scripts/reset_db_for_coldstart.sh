#!/usr/bin/env bash
# =============================================================================
# Reset the DB to "tickers only, nothing derived" — a true cold-start test of
# the full backfill chain, OHLCV -> email.
#
# THE CUT IS NOT "everything except tickers". It is:
#     wipe everything the BACKFILL REGENERATES
#     preserve everything it CANNOT
#
# Because the backfill pipeline does NOT populate these, and wiping them would
# silently produce an empty-but-"successful" run:
#
#   instrument_industry   ticker -> industry map. Written only by
#                         fundamentals_repo (a separate ETL). Without it,
#                         industry_index_build has no constituents, so
#                         industry_index_value / industry_state /
#                         industry_enrichment / sector_metrics / regime_matrix
#                         all produce NOTHING — and report success.
#   instrument_shares     shares outstanding, from yfinance get_shares_full.
#                         Without it the market_cap stage produces nothing.
#   industry_override     hand-curated industry corrections. Not reproducible.
#   subscribers /         WITHOUT THESE THERE IS NO EMAIL. The notify stage
#   subscriptions         iterates subscribers; zero subscribers = zero emails,
#                         and the stage "passes".
#   corporate_actions     fetched (splits/dividends). Expensive to refetch.
#   quarterly_financials  fetched. Expensive to refetch.
#   symbol_history        symbol renames. Historical record.
#   exchange_state        exchange config/state.
#   alembic_version       SCHEMA VERSION. Wipe this and alembic thinks NO
#   schema_migrations     migration has ever run. Never truncate these.
#   tickers               the ids/names you asked to keep.
#
# Everything else is derived and gets rebuilt.
#
# Usage:  bash reset_db_for_coldstart.sh          # dry run, shows counts
#         bash reset_db_for_coldstart.sh --apply  # actually truncates
# =============================================================================
set -euo pipefail

DB="${DB:-trip_ema}"
ROLE="${ROLE:-trip_etl_user}"          # needs TRUNCATE
READ_ROLE="${READ_ROLE:-trip_gui_user}"
APPLY="${1:-}"

# ---- Tables the backfill REGENERATES. Safe to wipe. ------------------------
DERIVED=(
  # raw bars (stage 1 refetches from yfinance)
  ohlcv_bars
  # stock indicator state + enrichment (stages 2/3, merged runner)
  stock_indicator_state
  stock_indicator_enrichment
  # market cap (stage 4)
  daily_market_cap
  # industry index scaffolding + values (stages 5, 3d)
  industry_index
  industry_index_constituent
  industry_index_period
  industry_index_value
  # industry state + enrichment (stages 5, 6)
  industry_indicator_state
  industry_indicator_enrichment
  # sector metrics + regime matrix (stages 6b, 6c)
  sector_stock_leadlag
  stock_benchmark_stats
  industry_benchmark_stats
  industry_regime_corr
  industry_regime_breadth
  influential_industry_ensemble
  # signals (stages 7, 8)
  historical_signals
  current_signals
  intraday_signal_log
  # ML enrichment (stages 8b, 8c, 8d)
  signal_enrichment
  signal_labels
  signal_ensemble_features
  # snapshots
  market_overview_daily
  # notification bookkeeping — MUST be cleared, or dedup/last_notified state
  # from previous sends can suppress the very email we are trying to observe
  notification_log
  # run ledgers / bookkeeping — clear so the phase ledger cannot RESUME and
  # skip stages as already COMPLETE. This is the whole point of a cold start.
  pipeline_phase_status
  backfill_runs
  compute_runs
  etl_runs
  etl_ticker_status
  fundamentals_run
  signal_backtest_runs
  update_run_in_progress
)

PRESERVE=(
  tickers instrument_industry instrument_shares industry_override
  subscribers subscriptions corporate_actions quarterly_financials
  symbol_history exchange_state alembic_version schema_migrations
)

echo "==================================================================="
echo " BEFORE — row counts"
echo "==================================================================="
printf '%-32s %12s\n' "TABLE" "ROWS"
for t in "${PRESERVE[@]}"; do
  n=$(psql -U "$READ_ROLE" -d "$DB" -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo "?")
  printf '  KEEP  %-24s %12s\n' "$t" "$n"
done
echo
for t in "${DERIVED[@]}"; do
  n=$(psql -U "$READ_ROLE" -d "$DB" -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo "?")
  printf '  WIPE  %-24s %12s\n' "$t" "$n"
done

if [[ "$APPLY" != "--apply" ]]; then
  echo
  echo "DRY RUN. Nothing changed."
  echo "Re-run with --apply to truncate the WIPE list."
  echo
  echo "SANITY CHECK FIRST — these must all be NON-ZERO above, or the backfill"
  echo "will 'succeed' while producing nothing:"
  echo "    tickers, instrument_industry, instrument_shares, subscribers"
  exit 0
fi

# Refuse to proceed if the things the backfill CANNOT rebuild are missing.
for t in tickers instrument_industry instrument_shares subscribers; do
  n=$(psql -U "$READ_ROLE" -d "$DB" -tAc "SELECT count(*) FROM $t")
  if [[ "$n" -eq 0 ]]; then
    echo "ABORT: $t is EMPTY. The backfill does not populate it, so the run"
    echo "       would complete 'successfully' while producing nothing."
    exit 1
  fi
done

echo
echo "==================================================================="
echo " TRUNCATING ${#DERIVED[@]} derived tables"
echo "==================================================================="
# One statement: TRUNCATE ... CASCADE in a single transaction, so FK order
# doesn't matter and it's all-or-nothing.
LIST=$(IFS=, ; echo "${DERIVED[*]}")
psql -U "$ROLE" -d "$DB" -v ON_ERROR_STOP=1 -c "
BEGIN;
TRUNCATE TABLE ${LIST} RESTART IDENTITY CASCADE;
COMMIT;
"

echo
echo "==================================================================="
echo " AFTER"
echo "==================================================================="
for t in tickers instrument_industry instrument_shares subscribers; do
  n=$(psql -U "$READ_ROLE" -d "$DB" -tAc "SELECT count(*) FROM $t")
  printf '  KEPT  %-24s %12s\n' "$t" "$n"
done
for t in ohlcv_bars stock_indicator_state historical_signals current_signals \
         pipeline_phase_status notification_log; do
  n=$(psql -U "$READ_ROLE" -d "$DB" -tAc "SELECT count(*) FROM $t")
  printf '  WIPED %-24s %12s\n' "$t" "$n"
done
echo
echo "Cold start ready."
