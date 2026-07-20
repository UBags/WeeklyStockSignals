#!/usr/bin/env bash
# =============================================================================
# backfill_from_stage.sh — re-run the backfill pipeline from a chosen stage.
#
#   Usage: bash backfill_from_stage.sh <EXCHANGE> <STAGE> [options] [-- cli args]
#          bash backfill_from_stage.sh --help
#          bash backfill_from_stage.sh --list
#
# WHY THIS EXISTS. `backfill()` records each completed phase in
# pipeline_phase_status and SKIPS anything already COMPLETE. That makes a
# resume cheap, but it also means "re-run from stage N" needs two things done
# together, and doing only one of them corrupts data:
#
#   1. DELETE the ledger rows for stage N onward, so the phases actually run.
#   2. DELETE the tables those phases write, because most of the pipeline
#      writes insert-if-missing (ON CONFLICT DO NOTHING). A phase that re-runs
#      over its own old rows keeps the STALE ones and quietly produces a
#      half-old, half-new table.
#
# Doing (1) without (2) is what broke the 2026-07-19 run: the rebalance chain
# rebuilt over live industry_index_constituent rows and inverted an effective
# period (iic_range_chk caught it — a constituent that leaves before it joins).
# This script always does both, in reverse dependency order.
#
# -----------------------------------------------------------------------------
# THE PIPELINE, IN ORDER
# -----------------------------------------------------------------------------
# Each line: <stage name>  —  what it does  →  the table(s) it owns.
# "owns" means this script DELETES those tables when the stage is in scope.
#
#  1 reference_industry     yfinance sector/industry per ticker (gaps only).
#                           → instrument_industry, fetch_status[industry]
#                           NETWORK. ~2,700 tickers at the configured gap.
#  2 reference_shares       yfinance sharesOutstanding (gaps only).
#                           → instrument_shares, fetch_status[shares]
#                           NETWORK. MUST precede market_cap.
#  3 reference_financials   yfinance quarterly revenue/EBITDA (gaps only).
#                           → quarterly_financials, fetch_status[financials]
#                           NETWORK. Slowest sweep: ~3h20m for a full universe
#                           at a 5s gap.
#  4 refresh_corp_actions   nselib corporate actions feed.
#                           → nse_corporate_actions            NETWORK (fast)
#  5 fetch_bars             yfinance OHLCV for every ticker, full history.
#                           → ohlcv_bars                       NETWORK. ~50 min.
#  6 correct_ohlcv          Split/seam correction against nse_corporate_actions.
#                           → (edits ohlcv_bars IN PLACE — nothing to delete;
#                              re-running is safe and idempotent)
#  7 stock_state            Triple-EMA + ATR per ticker per week.
#                           → stock_indicator_state
#  8 stock_enrichment       Derived per-state features.
#                           → stock_indicator_enrichment
#  9 market_cap             instrument_shares x ohlcv_bars.
#                           → daily_market_cap
# 10 industry_index_build   Annual-April chain-linked constituent selection.
#                           → industry_index_constituent, industry_index_period
# 11 industry_index_values  Daily index level per industry.
#                           → industry_index_value
# 12 industry_state         Triple-EMA + ATR on each industry index.
#                           → industry_indicator_state
# 13 industry_enrichment    Derived per-industry-state features.
#                           → industry_indicator_enrichment
# 14 sector_metrics         Per ticker x week sector-relative metrics and the
#                           21-lag rolling lead/lag scan.
#                           → sector_stock_leadlag              CPU-HEAVY
# 15 regime_matrix          Industry x industry regime correlation, breadth,
#                           and the influential-industry ensembles.
#                           → industry_regime_corr,
#                             industry_regime_breadth,
#                             influential_industry_ensemble
# 16 benchmark_stats        Alpha/beta/RS rankings.
#                           → stock_benchmark_stats, industry_benchmark_stats
# 17 delete_historical      Clears historical_signals in the run's date range
#                           so daywalk can rewrite it. Owns no table of its own.
# 18 daywalk                Emits signals on every consecutive state-row pair
#                           whose regime differs.
#                           → historical_signals, current_signals
# 19 signal_enrichment      Per-signal feature store (the model's X).
#                           → signal_enrichment
# 20 signal_labels          Per-signal outcomes incl. the forward walk (y).
#                           → signal_labels
# 21 signal_ensemble_features
#                           Cross-industry confirmation per signal.
#                           → signal_ensemble_features
#
# NOT LEDGERED, always run at the end: stage_8e forward-walk (writes into
# signal_labels), stage_9 postflight audit, notify.
#
# scoped_shares_refresh appears only in ticker-scoped runs (--stocks) and is
# not part of a full-universe pipeline, so it is not selectable here.
#
# -----------------------------------------------------------------------------
# CHOOSING A STAGE
# -----------------------------------------------------------------------------
# Pick the EARLIEST stage whose INPUT changed. Some worked examples:
#
#   changed the indicator math          -> stock_state
#   changed industry index construction  -> industry_index_build
#   changed the lead/lag computation     -> sector_metrics
#   changed signal enrichment features   -> signal_enrichment
#   changed forward-walk return maths    -> signal_labels
#   lowered industry_index_min_constituents -> industry_index_build
#   repaired bars in place (SQL UPDATE)  -> stock_state
#   refetched or corrected bars          -> correct_ohlcv
#
# If unsure, go one stage EARLIER. Re-running a stage that did not need it
# costs time; skipping one that did leaves the database internally
# inconsistent, and nothing downstream will tell you.
#
# -----------------------------------------------------------------------------
# OPTIONS
# -----------------------------------------------------------------------------
#   --dry-run        Print every DELETE and the CLI command, change nothing.
#   --yes            Skip the confirmation prompt (for unattended runs).
#   --date-from D    Backfill start date. Default 2006-04-01.
#   --keep-bars      Do NOT delete ohlcv_bars even when fetch_bars is in scope.
#                    Use when you want the fetch stage to top up rather than
#                    start from nothing.
#   --fg             Run in the foreground. Default is setsid+nohup, detached,
#                    so an SSH drop does not kill a two-hour run.
#   --list           Print the stage list and exit.
#   --help           Print this header and exit.
#
# Anything after `--` is forwarded verbatim to `trip-ema-etl backfill`.
#
# --no_fetch_bars is added AUTOMATICALLY when the chosen stage is after
# fetch_bars, so a compute-only re-run cannot accidentally spend 50 minutes
# refetching bars it already has. Pass it yourself to be explicit; passing it
# twice is harmless.
#
# EXAMPLES
#   bash backfill_from_stage.sh NSE sector_metrics
#   bash backfill_from_stage.sh NSE industry_index_build --dry-run
#   bash backfill_from_stage.sh NSE stock_state --yes --date-from 2010-01-01
#   bash backfill_from_stage.sh NSE signal_labels -- --stocks RELIANCE TCS
# =============================================================================
set -uo pipefail

REPO="$HOME/PycharmProjects/WeeklyStockSignals"
PY="$REPO/.venv/bin/python"
RO_USER=trip_gui_user
RW_USER=trip_etl_user
DB=trip_ema

# ---- the pipeline, in execution order -------------------------------------
STAGES=(
  reference_industry reference_shares reference_financials
  refresh_corp_actions fetch_bars correct_ohlcv
  stock_state stock_enrichment market_cap
  industry_index_build industry_index_values industry_state
  industry_enrichment sector_metrics regime_matrix benchmark_stats
  delete_historical daywalk
  signal_enrichment signal_labels signal_ensemble_features
)

# Tables each stage owns. Empty = the stage owns no table (it edits in place or
# only deletes). Space-separated; deleted in REVERSE pipeline order so a child
# table never outlives its parent (signal_labels and signal_ensemble_features
# both carry an FK to signal_enrichment).
declare -A OWNS=(
  [reference_industry]="instrument_industry"
  [reference_shares]="instrument_shares"
  [reference_financials]="quarterly_financials"
  [refresh_corp_actions]="nse_corporate_actions"
  [fetch_bars]="ohlcv_bars"
  [correct_ohlcv]=""
  [stock_state]="stock_indicator_state"
  [stock_enrichment]="stock_indicator_enrichment"
  [market_cap]="daily_market_cap"
  [industry_index_build]="industry_index_constituent industry_index_period"
  [industry_index_values]="industry_index_value"
  [industry_state]="industry_indicator_state"
  [industry_enrichment]="industry_indicator_enrichment"
  [sector_metrics]="sector_stock_leadlag"
  [regime_matrix]="industry_regime_corr industry_regime_breadth influential_industry_ensemble"
  [benchmark_stats]="stock_benchmark_stats industry_benchmark_stats"
  [delete_historical]=""
  [daywalk]="historical_signals current_signals"
  [signal_enrichment]="signal_enrichment"
  [signal_labels]="signal_labels"
  [signal_ensemble_features]="signal_ensemble_features"
)

# fundamentals_fetch_status is keyed by dataset, so the three reference sweeps
# clear only their own slice. Without this a re-swept ticker stays UNAVAILABLE
# and the gaps-only pass skips it forever.
declare -A FETCH_DATASET=(
  [reference_industry]=industry
  [reference_shares]=shares
  [reference_financials]=financials
)

usage() { sed -n '2,/^set -uo/p' "$0" | sed 's/^# \{0,1\}//' | sed '$d'; }

list_stages() {
  echo "Pipeline stages, in order:"
  local i=1
  for s in "${STAGES[@]}"; do
    printf "  %2d  %-26s -> %s\n" "$i" "$s" "${OWNS[$s]:-(edits in place)}"
    i=$((i + 1))
  done
}

case "${1:-}" in
  --help|-h|"") usage; exit 0 ;;
  --list|-l)    list_stages; exit 0 ;;
esac

EXCHANGE="$1"; shift
STAGE="${1:-}"; shift || true
if [[ -z "$STAGE" || "$STAGE" == --* ]]; then
  echo "error: a STAGE is required." >&2
  echo "usage: bash $(basename "$0") <EXCHANGE> <STAGE> [options]" >&2
  echo "run with --list to see the stages, --help for the full pipeline." >&2
  exit 2
fi

DRY=0; ASSUME_YES=0; DATE_FROM=2006-04-01; KEEP_BARS=0; FOREGROUND=0
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   DRY=1; shift ;;
    --yes|-y)    ASSUME_YES=1; shift ;;
    --keep-bars) KEEP_BARS=1; shift ;;
    --fg)        FOREGROUND=1; shift ;;
    --date-from) DATE_FROM="${2:?--date-from needs a value}"; shift 2 ;;
    --)          shift; EXTRA=("$@"); break ;;
    *) echo "error: unknown option '$1' (use -- to forward CLI args)" >&2; exit 2 ;;
  esac
done

# ---- resolve the stage to an index ----------------------------------------
START=-1
for i in "${!STAGES[@]}"; do
  [[ "${STAGES[$i]}" == "$STAGE" ]] && START=$i && break
done
if [[ $START -lt 0 ]]; then
  echo "error: '$STAGE' is not a pipeline stage." >&2
  echo >&2
  list_stages >&2
  exit 2
fi

SCOPE=("${STAGES[@]:$START}")

# ---- auto --no_fetch_bars when bars are not being refetched ---------------
FETCH_IDX=-1
for i in "${!STAGES[@]}"; do
  [[ "${STAGES[$i]}" == "fetch_bars" ]] && FETCH_IDX=$i && break
done
AUTO_NFB=0
if [[ $START -gt $FETCH_IDX ]]; then
  for a in "${EXTRA[@]:-}"; do [[ "$a" == "--no_fetch_bars" ]] && AUTO_NFB=-1; done
  [[ $AUTO_NFB -eq 0 ]] && { EXTRA+=("--no_fetch_bars"); AUTO_NFB=1; }
fi

# ---- build the DELETE list, reverse dependency order ----------------------
DELETES=()
for (( i=${#SCOPE[@]}-1 ; i>=0 ; i-- )); do
  s="${SCOPE[$i]}"
  for t in ${OWNS[$s]:-}; do
    [[ "$t" == "ohlcv_bars" && $KEEP_BARS -eq 1 ]] && continue
    DELETES+=("DELETE FROM $t;")
  done
  ds="${FETCH_DATASET[$s]:-}"
  [[ -n "$ds" ]] && \
    DELETES+=("DELETE FROM fundamentals_fetch_status WHERE dataset = '$ds';")
done

LEDGER_LIST=$(printf "'%s'," "${SCOPE[@]}"); LEDGER_LIST="${LEDGER_LIST%,}"

# ---- show the plan ---------------------------------------------------------
cat <<BANNER
===================================================================
 BACKFILL FROM STAGE -- ${EXCHANGE}
===================================================================
 from stage : ${STAGE}  (#$((START + 1)) of ${#STAGES[@]})
 stages     : ${#SCOPE[@]} will run
 date_from  : ${DATE_FROM}
 extra args : ${EXTRA[*]:-(none)}
BANNER
[[ $AUTO_NFB -eq 1 ]] && echo " note       : --no_fetch_bars added automatically (bars are before this stage)"
[[ $KEEP_BARS -eq 1 ]] && echo " note       : --keep-bars set; ohlcv_bars will NOT be deleted"
echo "==================================================================="
echo
echo "Stages to run:"
printf '  %s\n' "${SCOPE[@]}"
echo
echo "Tables to be EMPTIED (reverse dependency order):"
if [[ ${#DELETES[@]} -eq 0 ]]; then
  echo "  (none — this stage edits in place)"
else
  printf '  %s\n' "${DELETES[@]}"
fi
echo
echo "Ledger rows to clear: ${LEDGER_LIST}"
echo

# ---- current row counts, so the damage is visible before it happens -------
if [[ ${#DELETES[@]} -gt 0 ]]; then
  echo "Current row counts:"
  for d in "${DELETES[@]}"; do
    tbl=$(echo "$d" | sed -E 's/DELETE FROM ([a-z_]+).*/\1/')
    n=$(psql -U "$RO_USER" -d "$DB" -tAc "SELECT count(*) FROM $tbl;" 2>/dev/null)
    printf "  %-34s %s\n" "$tbl" "${n:-?}"
  done
  echo
fi

# ---- cost warning: network stages are hours, not minutes -----------------
NETWORK_IN_SCOPE=()
for s in "${SCOPE[@]}"; do
  case "$s" in
    reference_industry|reference_shares|reference_financials|fetch_bars|refresh_corp_actions)
      NETWORK_IN_SCOPE+=("$s") ;;
  esac
done
if [[ ${#NETWORK_IN_SCOPE[@]} -gt 0 ]]; then
  GAP=$(grep -E '^ *fundamentals_min_request_gap_seconds:' "$REPO/config.yaml" \
        | head -1 | sed -E 's/[^0-9.]*([0-9.]+).*/\1/')
  echo "!!! NETWORK STAGES IN SCOPE: ${NETWORK_IN_SCOPE[*]}"
  echo "    These re-fetch from yfinance under the global request gap"
  echo "    (fundamentals_min_request_gap_seconds = ${GAP:-?}s), so cost is"
  echo "    roughly tickers x gap and is measured in HOURS, not minutes."
  echo "    A full financials sweep at 5s is ~3h20m."
  echo
  echo "    Deleting fundamentals_fetch_status also revokes every UNAVAILABLE"
  echo "    verdict, so tickers the provider genuinely has nothing for will be"
  echo "    asked all over again (3x each, via the retry layers)."
  echo
  echo "    If you only want to fill GAPS, stop and use instead:"
  echo "      $PY -m trip_ema.etl.cli refresh-shares --exchange $EXCHANGE --only-missing"
  echo "      $PY -m trip_ema.etl.cli refresh-financials --exchange $EXCHANGE --only-missing"
  echo
fi

if [[ $DRY -eq 1 ]]; then
  echo "--dry-run: nothing was changed."
  echo
  echo "Would run:"
  echo "  $PY -m trip_ema.etl.cli backfill --exchange $EXCHANGE" \
       "--date_from $DATE_FROM ${EXTRA[*]:-}"
  exit 0
fi

if [[ $ASSUME_YES -ne 1 ]]; then
  echo "This DELETES the rows listed above. They are rebuilt by the run that"
  echo "follows — but if that run fails, the database is left incomplete."
  read -r -p "Type the stage name to confirm: " reply
  if [[ "$reply" != "$STAGE" ]]; then
    echo "aborted (got '$reply', expected '$STAGE')."
    exit 1
  fi
fi

# ---- 1. release any orphan lock -------------------------------------------
echo
echo "--- releasing backfill lock ---"
"$PY" -m trip_ema.etl.cli backfill --exchange "$EXCHANGE" --release-lock

# ---- 2. refuse to proceed if a backfill is still alive --------------------
if pgrep -f "trip_ema.etl.cli backfill" >/dev/null 2>&1; then
  echo
  echo "ERROR: a backfill process is still running:" >&2
  pgrep -af "trip_ema.etl.cli backfill" >&2
  echo "Two concurrent backfills interleave writes to the same tables." >&2
  echo "Kill it, then re-run this script." >&2
  exit 3
fi

# ---- 3. clear the tables, then the ledger, in ONE transaction -------------
# One transaction so a failure mid-way cannot leave tables emptied while the
# ledger still claims the stages are COMPLETE — the state in which the next
# run silently skips the work needed to refill them.
echo
echo "--- clearing derived tables + ledger ---"
{
  echo "BEGIN;"
  printf '%s\n' "${DELETES[@]:-}"
  echo "DELETE FROM pipeline_phase_status"
  echo " WHERE exchange = '${EXCHANGE}' AND phase_name IN (${LEDGER_LIST});"
  echo "COMMIT;"
} | psql -U "$RW_USER" -d "$DB" -v ON_ERROR_STOP=1
CLEAR_RC=$?
if [[ $CLEAR_RC -ne 0 ]]; then
  echo "ERROR: clear failed (rc=$CLEAR_RC), transaction rolled back." >&2
  echo "Nothing was deleted and the ledger is untouched. Safe to retry." >&2
  exit 4
fi

# ---- 4. show what the ledger looks like now -------------------------------
echo
echo "--- ledger after clear (these will be SKIPPED as already done) ---"
psql -U "$RO_USER" -d "$DB" -c "
SELECT phase_name, status,
       to_char(completed_at AT TIME ZONE 'Asia/Kolkata','MM-DD HH24:MI') AS done_ist
  FROM pipeline_phase_status
 WHERE exchange = '${EXCHANGE}' ORDER BY started_at;"

# ---- 5. run --------------------------------------------------------------
LOG=/tmp/backfill_from_${STAGE}_$(date +%Y%m%d_%H%M%S).log
echo
echo "--- starting backfill from '${STAGE}' ---"
echo "log: $LOG"
cd "$REPO" || exit 5

if [[ $FOREGROUND -eq 1 ]]; then
  "$PY" -m trip_ema.etl.cli backfill \
      --exchange "$EXCHANGE" --date_from "$DATE_FROM" \
      "${EXTRA[@]:-}" 2>&1 | tee "$LOG"
  exit "${PIPESTATUS[0]}"
fi

setsid nohup "$PY" -m trip_ema.etl.cli backfill \
    --exchange "$EXCHANGE" --date_from "$DATE_FROM" \
    "${EXTRA[@]:-}" > "$LOG" 2>&1 < /dev/null &

sleep 20
if pgrep -f "trip_ema.etl.cli backfill" >/dev/null 2>&1; then
  echo "running, detached:"
  pgrep -af "trip_ema.etl.cli backfill"
  echo
  echo "  tail -f $LOG"
else
  echo "ERROR: the process died within 20s. Last of the log:" >&2
  tail -30 "$LOG" >&2
  exit 6
fi
