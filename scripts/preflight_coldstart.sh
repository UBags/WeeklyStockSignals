#!/usr/bin/env bash
# =============================================================================
# COLD-START PREFLIGHT
#
# Every one of these can make a 6-hour backfill complete "successfully" while
# producing nothing useful. Check them BEFORE you start, not after.
#
# Usage: bash preflight_coldstart.sh
# =============================================================================
set -uo pipefail
DB="${DB:-trip_ema}"
R="${READ_ROLE:-trip_gui_user}"
FAIL=0

q() { psql -U "$R" -d "$DB" -tAc "$1" 2>/dev/null; }

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=1; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }

echo "==================================================================="
echo " COLD-START PREFLIGHT"
echo "==================================================================="
echo
echo "-- Inputs the backfill CANNOT rebuild ------------------------------"
for t in tickers instrument_industry instrument_shares subscribers; do
  n=$(q "SELECT count(*) FROM $t")
  if [[ "${n:-0}" -gt 0 ]]; then ok "$t = $n"
  else bad "$t is EMPTY — backfill will 'succeed' producing nothing"; fi
done

echo
echo "-- THE EMAIL GATES (each one silently sends nothing) ---------------"

# 1. is_active
n=$(q "SELECT count(*) FROM subscribers WHERE is_active = TRUE")
if [[ "${n:-0}" -gt 0 ]]; then ok "active subscribers = $n"
else bad "NO ACTIVE subscribers — find_due_for_notification returns []"; fi

# 2. cadence / last_notified_at  <-- the one that bites on a cold start
#    subscribers is PRESERVED by the reset (you need the row), so
#    last_notified_at survives. If it is inside notify_cadence_minutes the
#    subscriber is NOT DUE and no email is sent.
DUE=$(q "
  SELECT count(*) FROM subscribers
   WHERE is_active = TRUE
     AND (last_notified_at IS NULL
          OR last_notified_at <= NOW() - make_interval(mins => notify_cadence_minutes))")
if [[ "${DUE:-0}" -gt 0 ]]; then
  ok "subscribers DUE for notification = $DUE"
else
  bad "NO subscriber is DUE. last_notified_at is inside notify_cadence_minutes,"
  echo "        so find_due_for_notification returns [] and NO EMAIL IS SENT —"
  echo "        after the whole backfill completes 'successfully'."
  echo
  q "SELECT '        subscriber_id=' || subscriber_id
          || '  is_active=' || is_active
          || '  cadence_min=' || notify_cadence_minutes
          || '  last_notified_at=' || COALESCE(last_notified_at::text, 'NULL')
       FROM subscribers"
  echo
  echo "        FIX (makes them 'never notified', i.e. due):"
  echo "          psql -U trip_etl_user -d $DB -c \\"
  echo "            \"UPDATE subscribers SET last_notified_at = NULL;\""
fi

# 3. NIFTYBEES — the NIFTY benchmark leg of stage 6d
n=$(q "SELECT count(*) FROM tickers WHERE symbol = 'NIFTYBEES'")
if [[ "${n:-0}" -gt 0 ]]; then ok "NIFTYBEES present (NIFTY benchmark leg of 6d)"
else warn "NIFTYBEES absent — stage 6d will SKIP every NIFTY benchmark. Not fatal:
        INDUSTRY stats still compute, but rank_score loses the NIFTY component."; fi

# 4. Brevo transport — pipeline skips notify entirely if unconfigured
if [[ -f secrets/brevo.yaml ]]; then
  PERM=$(stat -c '%a' secrets/brevo.yaml)
  if [[ "$PERM" == "600" ]]; then ok "secrets/brevo.yaml present (chmod $PERM)"
  else warn "secrets/brevo.yaml is chmod $PERM — the loader fail-fasts unless 600"; fi
else
  bad "secrets/brevo.yaml MISSING — pipeline logs 'skipped: brevo not configured'
        and sends no email, WITHOUT failing the run."
fi

echo
echo "-- Resume ledger (must be empty, or stages are skipped as COMPLETE) -"
n=$(q "SELECT count(*) FROM pipeline_phase_status WHERE exchange='NSE'")
if [[ "${n:-0}" -eq 0 ]]; then ok "pipeline_phase_status empty — every stage will run"
else bad "pipeline_phase_status has $n row(s) — resume will SKIP stages as
        COMPLETE, and if daywalk is skipped the pipeline forces
        today_walked = False, so NO EMAIL. Run the reset script."; fi

echo
echo "-- Trading-day / effective_today ------------------------------------"
DOW=$(date '+%A')
LAST=$(q "SELECT max(date) FROM stock_indicator_state")
echo "        today = $(date '+%Y-%m-%d') ($DOW)"
if [[ "$DOW" == "Saturday" || "$DOW" == "Sunday" ]]; then
  warn "non-trading day. With the EFFECTIVE-TODAY fix, current_signals is built
        from the last trading day and the email still fires. WITHOUT that fix it
        would produce zero current_signals and no email. Confirm the fix is in:
          grep -c EFFECTIVE-TODAY trip_ema/backfill/bulk_daywalk.py   # must be >0"
else
  ok "trading day"
fi

echo
echo "==================================================================="
if [[ "$FAIL" -eq 0 ]]; then
  echo " PREFLIGHT PASSED — safe to run the cold start."
else
  echo " PREFLIGHT FAILED — fix the items above first."
fi
echo "==================================================================="
exit "$FAIL"
