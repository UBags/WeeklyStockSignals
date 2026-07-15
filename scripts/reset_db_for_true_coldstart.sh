#!/usr/bin/env bash
# =============================================================================
# TRUE cold start — wipe EVERYTHING the pipeline can rebuild, including the
# reference layer (tickers, instrument_industry, instrument_shares). Tests the
# full self-bootstrap: point at an exchange, backfill, and watch the whole chain
# build from nothing.
#
# THE CUT: keep only what the backfill CANNOT reconstruct AND is not
# reproducible / must never be dropped:
#
#     subscribers        the email target. notify_subscribers keys off THIS
#                        (+ current_signals), NOT subscriptions, so the email
#                        still fires after the wipe. No subscriber => no email.
#     exchange_state     per-exchange config/state. Has no ticker FK, so it
#                        survives the tickers cascade anyway; kept explicitly.
#     alembic_version    SCHEMA VERSION. Wipe it and alembic thinks no migration
#     schema_migrations  ever ran. NEVER truncate these.
#
# Everything else (41 tables) is wiped. What the backfill REBUILDS:
#     tickers                <- stage 0  refresh_listings (INSERT new listings)
#     instrument_industry    <- stage 0b run_industry_refresh (only_missing=all)
#     instrument_shares      <- stage 0c run_shares_refresh
#     quarterly_financials   <- stage 0d run_financials_refresh
#     daily_market_cap / ohlcv_bars / all state / signals / enrichment / index
#
# What is wiped and NOT rebuilt by the backfill — harmless for this test:
#     corporate_actions      written by the ETL reconciler, READ by nothing on
#                            the compute path (yfinance adjusted close handles
#                            splits). Repopulates over time.
#     subscriptions          per-subscriber ticker prefs; the current notifier
#                            does not read it, and it would dangle against the
#                            regenerated ticker_ids anyway.
#     symbol_history         rename log. Rebuilds as renames are observed.
#     fundamentals_fetch_status  provider "no data" verdicts. Starting empty is
#                            CLEANER — nothing is pre-skipped on the first sweep.
#     industry_override      hand-curated corrections (currently 0 rows).
#
# NB: TRUNCATE does NOT RESTART IDENTITY (trip_etl_user is not the sequence
# owner). New ticker_ids continue from the old max — harmless; every key is
# natural, nothing depends on ids restarting at 1. To reset sequences too, run
# as the owner: ROLE=trip_ema_app bash reset_db_for_true_coldstart.sh --apply
#
# Usage:  bash reset_db_for_true_coldstart.sh          # dry run, shows counts
#         bash reset_db_for_true_coldstart.sh --apply  # actually truncates
# =============================================================================
set -euo pipefail

DB="${DB:-trip_ema}"
ROLE="${ROLE:-trip_etl_user}"          # needs TRUNCATE on every wiped table
READ_ROLE="${READ_ROLE:-trip_gui_user}"
APPLY="${1:-}"

# ---- KEEP: the only survivors ----------------------------------------------
PRESERVE=(subscribers exchange_state alembic_version schema_migrations)

# ---- WIPE: all 41 others. CASCADE handles FK order; the list is explicit so
#      nothing is left behind by a missed dependency. -------------------------
WIPE=(
  backfill_runs compute_runs corporate_actions current_signals daily_market_cap
  etl_runs etl_ticker_status fundamentals_fetch_status fundamentals_run
  historical_signals industry_benchmark_stats industry_index
  industry_index_constituent industry_index_period industry_index_value
  industry_indicator_enrichment industry_indicator_state industry_override
  industry_regime_breadth industry_regime_corr influential_industry_ensemble
  instrument_industry instrument_shares intraday_signal_log market_overview_daily
  notification_log ohlcv_bars pipeline_phase_status quarterly_financials
  sector_stock_leadlag signal_backtest_runs signal_enrichment
  signal_ensemble_features signal_labels stock_benchmark_stats
  stock_indicator_enrichment stock_indicator_state subscriptions symbol_history
  tickers update_run_in_progress
)

echo "==================================================================="
echo " TRUE COLD START — BEFORE row counts"
echo "==================================================================="
printf '%-32s %12s\n' "TABLE" "ROWS"
for t in "${PRESERVE[@]}"; do
  n=$(psql -U "$READ_ROLE" -d "$DB" -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo "?")
  printf '  KEEP  %-24s %12s\n' "$t" "$n"
done
echo
for t in "${WIPE[@]}"; do
  n=$(psql -U "$READ_ROLE" -d "$DB" -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo "?")
  printf '  WIPE  %-24s %12s\n' "$t" "$n"
done

if [[ "$APPLY" != "--apply" ]]; then
  echo
  echo "DRY RUN. Nothing changed."
  echo "Re-run with --apply to truncate the WIPE list."
  echo
  echo "This wipes the REFERENCE layer too (tickers/industry/shares). The"
  echo "backfill rebuilds those for NSE from the listings + yfinance. Expect the"
  echo "reference sweeps (industry/shares/financials) to run the SLOW path now —"
  echo "they are no longer gap-only, they fetch every ticker (~2s each)."
  echo
  echo "MUST be non-zero above, or there is no email at the end:"
  echo "    subscribers"
  exit 0
fi

# The one survivor the run actually needs at the finish line.
n=$(psql -U "$READ_ROLE" -d "$DB" -tAc "SELECT count(*) FROM subscribers")
if [[ "$n" -eq 0 ]]; then
  echo "ABORT: subscribers is EMPTY. The backfill does not create subscribers,"
  echo "       so the run would complete and email nobody. Seed a subscriber"
  echo "       first, then re-run."
  exit 1
fi

echo
echo "==================================================================="
echo " TRUNCATING ${#WIPE[@]} tables (CASCADE, one transaction)"
echo "==================================================================="
LIST=$(IFS=, ; echo "${WIPE[*]}")
psql -U "$ROLE" -d "$DB" -v ON_ERROR_STOP=1 -c "
BEGIN;
TRUNCATE TABLE ${LIST} CASCADE;
COMMIT;
"

# ---- Reset subscriber notification cadence (last_notified_at survives the
#      wipe; find_due_for_notification would otherwise still consider them
#      recently-notified and send nothing). ---------------------------------
echo
echo "==================================================================="
echo " Resetting subscriber notification cadence"
echo "==================================================================="
psql -U "$ROLE" -d "$DB" -v ON_ERROR_STOP=1 -c "
UPDATE subscribers SET last_notified_at = NULL;
"
DUE=$(psql -U "$READ_ROLE" -d "$DB" -tAc "
  SELECT count(*) FROM subscribers
   WHERE is_active = TRUE
     AND (last_notified_at IS NULL
          OR last_notified_at <= NOW() - make_interval(mins => notify_cadence_minutes))")
echo "  last_notified_at = NULL  -> subscribers DUE for notification: $DUE"
if [[ "${DUE:-0}" -eq 0 ]]; then
  echo "  WARNING: still 0 due — check is_active. No email will be sent."
  psql -U "$READ_ROLE" -d "$DB" -c \
    "SELECT subscriber_id, email, is_active, notify_cadence_minutes,
            last_notified_at FROM subscribers;"
fi

echo
echo "==================================================================="
echo " AFTER"
echo "==================================================================="
for t in subscribers exchange_state; do
  n=$(psql -U "$READ_ROLE" -d "$DB" -tAc "SELECT count(*) FROM $t")
  printf '  KEPT  %-24s %12s\n' "$t" "$n"
done
for t in tickers instrument_industry instrument_shares ohlcv_bars \
         stock_indicator_state historical_signals current_signals \
         pipeline_phase_status notification_log; do
  n=$(psql -U "$READ_ROLE" -d "$DB" -tAc "SELECT count(*) FROM $t")
  printf '  WIPED %-24s %12s\n' "$t" "$n"
done

echo
echo "Empty exchange. Point at NSE and backfill:"
echo "    bash scripts/run_coldstart_timed.sh"
