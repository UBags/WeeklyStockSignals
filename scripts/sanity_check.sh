#!/usr/bin/env bash
# sanity_check.sh
#
# Post-restart / runtime sanity check for the trip-ema service.
#
# WHAT THIS DOES
#   1. Confirms the service process is alive and bound to its port.
#   2. Computes a time window from service-start (or a user-supplied
#      timestamp) to now.
#   3. Counts errors of interest WITHIN that window:
#        - Compute errors (live ComputeScheduler write path)
#        - ImportError / Traceback (anything blowing up at thread boot)
#        - Brevo / email transport failures
#   4. Verifies background threads have ticked at least once in the
#      window:
#        - MarketOrchestrator pipelines (`pipeline complete`)
#        - SubscriberDistributor (`Distribution run completed`)
#   5. Cross-checks DB state against expectations:
#        - signal_events row counts (today + week-to-date)
#        - notification_log activity (recent sends)
#        - indicator_state freshness (most recent computed_at)
#   6. Prints a one-line PASS / FAIL / WARN summary at the end.
#
# WHEN TO USE
#   - Immediately after `./scripts/restart_service.sh` to verify the
#     restart took cleanly (use --since-restart, the default).
#   - At end-of-day to confirm the EoD compute cycle and distribution
#     fired correctly.
#   - Any time you suspect something's drifted.
#
# USAGE
#   ./scripts/sanity_check.sh                         # default: since service start
#   ./scripts/sanity_check.sh --since '1 hour ago'    # last hour
#   ./scripts/sanity_check.sh --since '2026-06-24 03:00:00'  # explicit cutoff
#   ./scripts/sanity_check.sh --quiet                 # only summary line + exit code
#
# EXIT CODE
#   0  all checks PASS (or only WARN)
#   1  one or more checks FAIL
#   2  service not running
#
set -uo pipefail

# ===== Configuration =====
SERVICE_PORT="${SERVICE_PORT:-8000}"
SERVICE_LOG="${SERVICE_LOG:-/tmp/trip-ema.log}"
SERVICE_NAME="${SERVICE_NAME:-trip-ema}"
PG_USER_ETL="${PG_USER_ETL:-trip_etl_user}"
PG_USER_ENGINE="${PG_USER_ENGINE:-trip_engine_user}"
PG_DB="${PG_DB:-trip_ema}"

# Thresholds (override via env to tune)
WARN_NO_ORCHESTRATOR_TICK_MIN="${WARN_NO_ORCHESTRATOR_TICK_MIN:-30}"
WARN_NO_DISTRIBUTOR_TICK_MIN="${WARN_NO_DISTRIBUTOR_TICK_MIN:-30}"
WARN_INDICATOR_STATE_STALE_HOURS="${WARN_INDICATOR_STATE_STALE_HOURS:-48}"
# =========================

QUIET=0
SINCE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --quiet|-q)        QUIET=1; shift ;;
        --since)           SINCE="$2"; shift 2 ;;
        --since-restart)   SINCE=""; shift ;;
        -h|--help)
            sed -n '2,40p' "$0" | sed 's/^# *//'
            exit 0
            ;;
        *)
            echo "Unknown arg: $1" >&2
            echo "Try --help" >&2
            exit 2
            ;;
    esac
done

# ===== Output helpers =====
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'
    GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'
    BLUE=$'\033[34m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; RESET=""
fi

PASS_COUNT=0; WARN_COUNT=0; FAIL_COUNT=0
FAIL_REASONS=()

say() {
    [[ $QUIET -eq 0 ]] && echo -e "$1"
}
section() {
    [[ $QUIET -eq 0 ]] && echo -e "\n${BOLD}${BLUE}━━ $1 ━━${RESET}"
}
pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    [[ $QUIET -eq 0 ]] && echo -e "  ${GREEN}✓ PASS${RESET}  $1"
}
warn() {
    WARN_COUNT=$((WARN_COUNT + 1))
    [[ $QUIET -eq 0 ]] && echo -e "  ${YELLOW}⚠ WARN${RESET}  $1"
}
fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAIL_REASONS+=("$1")
    [[ $QUIET -eq 0 ]] && echo -e "  ${RED}✗ FAIL${RESET}  $1"
}
info() {
    [[ $QUIET -eq 0 ]] && echo -e "  ${DIM}·${RESET}      $1"
}

# ===== 1. Process =====
section "Service process"

PID="$(pgrep -f "${SERVICE_NAME} --config" | head -1 || true)"
if [[ -z "$PID" ]]; then
    fail "no ${SERVICE_NAME} process found"
    [[ $QUIET -eq 1 ]] && echo "FAIL: service not running"
    say "\n${RED}Service is not running. Run ./scripts/restart_service.sh first.${RESET}"
    exit 2
fi
pass "process alive (PID $PID)"

# Process start time (Linux: /proc/<pid>/stat field 22 → jiffies since boot)
SERVICE_START_EPOCH="$(ps -o lstart= -p "$PID" | xargs -I{} date -d "{}" +%s 2>/dev/null || echo 0)"
SERVICE_UPTIME_SEC=$(( $(date +%s) - SERVICE_START_EPOCH ))
SERVICE_UPTIME_HUMAN="$(printf '%dh%dm%ds' $((SERVICE_UPTIME_SEC/3600)) $(((SERVICE_UPTIME_SEC%3600)/60)) $((SERVICE_UPTIME_SEC%60)))"
info "uptime: $SERVICE_UPTIME_HUMAN"

# Note: we used to do an `ss -tln` port-binding check here. We removed
# it because /healthz (next section) is the canonical "is the HTTP
# layer alive on this port" check. If /healthz responds, the port is
# bound; if not, the next section flags it. Parsing `ss` output across
# distro variants was a source of false negatives without adding
# information.

# Thread count (gross sanity — we expect ≥ 5 typically: uvicorn worker
# + 3 orchestrator threads + distributor + metrics + etl_executor)
THREAD_COUNT="$(ls /proc/$PID/task/ 2>/dev/null | wc -l)"
if [[ $THREAD_COUNT -ge 6 ]]; then
    pass "$THREAD_COUNT OS threads"
else
    warn "only $THREAD_COUNT OS threads (expected ≥ 6)"
fi

# Decide the time window
if [[ -z "$SINCE" ]]; then
    SINCE_EPOCH=$SERVICE_START_EPOCH
    SINCE_DESC="service start"
else
    SINCE_EPOCH="$(date -d "$SINCE" +%s 2>/dev/null || echo 0)"
    SINCE_DESC="$(date -d "@$SINCE_EPOCH" '+%Y-%m-%d %H:%M:%S')"
fi
SINCE_HUMAN="$(date -d "@$SINCE_EPOCH" '+%Y-%m-%d %H:%M:%S')"
NOW_HUMAN="$(date '+%Y-%m-%d %H:%M:%S')"
info "window: $SINCE_HUMAN → $NOW_HUMAN  ($SINCE_DESC)"

# Helper: filter log lines by timestamp ≥ SINCE_HUMAN.
# Service log uses 'YYYY-MM-DD HH:MM:SS,fff' which sorts
# lexicographically when sliced at second precision — so a string >=
# cutoff is correct.
LOG_SINCE() {
    [[ -f "$SERVICE_LOG" ]] || return 0
    awk -v cutoff="$SINCE_HUMAN" '$0 >= cutoff' "$SERVICE_LOG"
}

# ===== 2. HTTP =====
section "HTTP healthz"

HEALTHZ_BODY="$(curl -sf -m 5 "http://localhost:${SERVICE_PORT}/healthz" 2>/dev/null || true)"
if [[ -z "$HEALTHZ_BODY" ]]; then
    fail "/healthz did not respond within 5s"
else
    pass "/healthz responded"
    CACHE_SIZE="$(echo "$HEALTHZ_BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("cache_size", "?"))' 2>/dev/null || echo "?")"
    REGISTRY_SIZE="$(echo "$HEALTHZ_BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("registry_size", "?"))' 2>/dev/null || echo "?")"
    info "cache_size=$CACHE_SIZE  registry_size=$REGISTRY_SIZE"
    if [[ "$CACHE_SIZE" == "0" ]] || [[ "$REGISTRY_SIZE" == "0" ]]; then
        fail "cache or registry is empty (priming failed?)"
    fi
fi

# ===== 3. Errors in window =====
section "Errors in window"

COMPUTE_ERR_N="$(LOG_SINCE | grep -cE 'Compute error|ON CONFLICT' || true)"
IMPORT_ERR_N="$(LOG_SINCE | grep -cE 'ImportError|ModuleNotFoundError' || true)"
TRACEBACK_N="$(LOG_SINCE | grep -cE '^Traceback|Traceback \(most recent call last\)' || true)"
EMAIL_FAIL_N="$(LOG_SINCE | grep -cE 'Brevo.*failed|EmailTransport.*failed|email.*delivery_failures=[1-9]' || true)"

[[ "$COMPUTE_ERR_N" -eq 0 ]] && pass "zero Compute errors" || fail "$COMPUTE_ERR_N Compute error(s) in window"
[[ "$IMPORT_ERR_N" -eq 0 ]] && pass "zero ImportError" || fail "$IMPORT_ERR_N ImportError(s) in window"
[[ "$TRACEBACK_N" -eq 0 ]] && pass "zero Traceback" || fail "$TRACEBACK_N Traceback(s) in window"
[[ "$EMAIL_FAIL_N" -eq 0 ]] && pass "zero email delivery failures" || warn "$EMAIL_FAIL_N email delivery failure(s) in window"

# ===== 4. Background thread activity =====
section "Background thread activity (in window)"

# Orchestrator ticks (counting `pipeline complete` lines, which only
# emit on a fully-run cycle — fetch + compute + distribute).
ORCH_N="$(LOG_SINCE | grep -cE 'pipeline complete' || true)"
LAST_ORCH_LINE="$(LOG_SINCE | grep -E 'pipeline complete' | tail -1)"
LAST_ORCH_TS="$(echo "$LAST_ORCH_LINE" | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' || true)"

if [[ "$ORCH_N" -gt 0 ]]; then
    pass "$ORCH_N orchestrator pipeline complete(s)"
    if [[ -n "$LAST_ORCH_TS" ]]; then
        LAST_ORCH_EPOCH="$(date -d "$LAST_ORCH_TS" +%s 2>/dev/null || echo 0)"
        AGE_MIN=$(( ( $(date +%s) - LAST_ORCH_EPOCH ) / 60 ))
        info "last orchestrator tick: $LAST_ORCH_TS  (${AGE_MIN} min ago)"
        if [[ "$AGE_MIN" -gt $WARN_NO_ORCHESTRATOR_TICK_MIN ]]; then
            warn "no orchestrator tick in last $AGE_MIN min (threshold ${WARN_NO_ORCHESTRATOR_TICK_MIN}m); markets may be closed"
        fi
    fi
elif [[ $SERVICE_UPTIME_SEC -lt 900 ]]; then
    info "no orchestrator pipeline yet (service uptime <15 min; first tick may not have fired)"
else
    warn "no orchestrator pipeline complete in window; first tick should have fired by now"
fi

# Distributor ticks
DIST_N="$(LOG_SINCE | grep -cE 'Distribution run completed' || true)"
LAST_DIST_LINE="$(LOG_SINCE | grep -E 'Distribution run completed' | tail -1)"
LAST_DIST_TS="$(echo "$LAST_DIST_LINE" | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' || true)"

if [[ "$DIST_N" -gt 0 ]]; then
    pass "$DIST_N distributor cycle(s) completed"
    if [[ -n "$LAST_DIST_TS" ]]; then
        LAST_DIST_EPOCH="$(date -d "$LAST_DIST_TS" +%s 2>/dev/null || echo 0)"
        AGE_MIN=$(( ( $(date +%s) - LAST_DIST_EPOCH ) / 60 ))
        info "last distributor tick: $LAST_DIST_TS  (${AGE_MIN} min ago)"
        if [[ "$AGE_MIN" -gt $WARN_NO_DISTRIBUTOR_TICK_MIN ]]; then
            warn "no distributor tick in last $AGE_MIN min (threshold ${WARN_NO_DISTRIBUTOR_TICK_MIN}m)"
        fi
    fi
elif [[ $SERVICE_UPTIME_SEC -lt 900 ]]; then
    info "no distributor cycle yet (service uptime <15 min; first cycle may not have fired)"
else
    warn "no distributor cycle in window; should have fired by now"
fi

# ===== 5. DB state =====
section "Database state"

# Use trip_etl_user for read access to the analytical tables.
PSQL_RUN() {
    psql -U "$PG_USER_ETL" -d "$PG_DB" -tA -F'|' -c "$1" 2>/dev/null
}

# Most recent indicator_state row.
# Strip newlines only (not spaces) — the timestamp string contains an
# internal space (e.g. "2026-06-24 01:38:38.756861+05:30") which date -d
# needs to parse correctly.
LAST_IND="$(PSQL_RUN "SELECT MAX(computed_at)::text FROM indicator_state WHERE ticker_id IS NOT NULL;" | tr -d '\r\n')"
if [[ -n "$LAST_IND" ]]; then
    LAST_IND_EPOCH="$(date -d "$LAST_IND" +%s 2>/dev/null || echo 0)"
    AGE_H=$(( ( $(date +%s) - LAST_IND_EPOCH ) / 3600 ))
    if [[ "$AGE_H" -gt $WARN_INDICATOR_STATE_STALE_HOURS ]]; then
        warn "ticker indicator_state stale: latest computed_at ${AGE_H}h ago (threshold ${WARN_INDICATOR_STATE_STALE_HOURS}h)"
    else
        pass "ticker indicator_state fresh (latest ${AGE_H}h ago)"
    fi
else
    fail "could not read indicator_state from DB"
fi

# Signal events generated within the window.
# Use to_timestamp(epoch) instead of a string-formatted UTC time —
# unmarked timestamp strings get interpreted in the Postgres server's
# timezone (Asia/Kolkata here), which would silently widen the window
# by IST_OFFSET (5.5h) and pull in pre-window rows. Epoch seconds are
# absolute and unambiguous.
SIG_WIN="$(PSQL_RUN "SELECT COUNT(*) FROM signal_events WHERE computed_at >= to_timestamp($SINCE_EPOCH);" | tr -d '\r\n')"
info "signal_events written in window: ${SIG_WIN:-0}"

# Notification log activity in window
NOTIF_WIN="$(psql -U "$PG_USER_ENGINE" -d "$PG_DB" -tA -F'|' -c \
    "SELECT COUNT(*) FROM notification_log WHERE sent_at >= to_timestamp($SINCE_EPOCH);" 2>/dev/null | tr -d '\r\n')"
info "notification_log rows in window: ${NOTIF_WIN:-0}"

# Total signal events (overall scale check)
SIG_TOTAL="$(PSQL_RUN "SELECT COUNT(*) FROM signal_events WHERE ticker_id IS NOT NULL;" | tr -d '\r\n')"
info "signal_events total: ${SIG_TOTAL:-?}"

# ===== 6. Chart endpoint data integrity =====
# End-to-end check that the chart endpoint returns the LATEST bar with
# fully populated indicator overlays. Catches three classes of bug at once:
#   - indicator-compute regressions (EMAs / ATR NULL in DB)
#   - route-precedence / URL-mounting issues (404 / wrong handler)
#   - JOIN mismatches between table layouts (e.g. industry weekly-Monday
#     trade_date vs daily IIV trade_date)
section "Chart endpoint data integrity"

# Defaults are tickers/industries known to have long history. Override via
# env to pin different samples (e.g. SAMPLE_TICKER_SYM=RELIANCE).
SAMPLE_TICKER_EX="${SAMPLE_TICKER_EX:-NSE}"
SAMPLE_TICKER_SYM="${SAMPLE_TICKER_SYM:-BALAMINES}"
SAMPLE_INDUSTRY="${SAMPLE_INDUSTRY:-Specialty Chemicals}"

# Helper: fetch chart JSON, extract last bar, verify EMA/ATR/regime
# populated. Echoes one of:
#   PASS|<ts>|<regime>
#   FAIL_NULL|<missing-fields>|<ts>
#   FAIL_EMPTY
#   FAIL_HTTP|<status>
#   FAIL_PARSE
_chart_check_last_bar() {
    local url="$1"
    local http_code
    local raw
    local body
    # -w writes the HTTP status on its own line at end; we split body/code below
    raw=$(curl -sS -m 10 -w '\n__HTTP_STATUS__%{http_code}' "$url" 2>/dev/null || echo $'\n__HTTP_STATUS__000')
    http_code="${raw##*__HTTP_STATUS__}"
    body="${raw%$'\n'__HTTP_STATUS__*}"

    if [[ "$http_code" != "200" ]]; then
        echo "FAIL_HTTP|$http_code"
        return
    fi
    # Pass body via env var, NOT stdin. Using `python3 - <<'PY'` with a
    # pipe creates a stdin conflict: the heredoc wins as stdin (python3
    # reads its script from it), the pipe gets SIGPIPE, and with
    # pipefail the whole pipeline exits non-zero — firing the `||` echo
    # as a phantom second FAIL_PARSE on top of Python's actual one.
    # Env-var passing sidesteps both problems.
    BODY="$body" python3 <<'PY' 2>/dev/null || echo "FAIL_PARSE"
import json, os, sys
try:
    d = json.loads(os.environ.get("BODY", ""))
except Exception:
    print("FAIL_PARSE")
    sys.exit(0)
bars = d.get("bars") or []
if not bars:
    print("FAIL_EMPTY")
    sys.exit(0)
last = bars[-1]
required = ["med_ema", "long_ema", "longest_ema", "atr"]
missing = [f for f in required if last.get(f) is None]
ts = last.get("bar_timestamp", "?")
if missing:
    print(f"FAIL_NULL|{','.join(missing)}|{ts}")
else:
    reg = last.get("display_bg", "?")
    print(f"PASS|{ts}|{reg}")
PY
}

# Helper: present a chart-check result to pass/warn/fail.
_present_chart_result() {
    local label="$1"
    local result="$2"
    case "$result" in
        PASS\|*)
            local ts="${result#PASS|}"; ts="${ts%%|*}"
            local reg="${result##*|}"
            pass "$label: last bar fully populated ($ts, regime=$reg)"
            ;;
        FAIL_NULL\|*)
            local rest="${result#FAIL_NULL|}"
            local fields="${rest%%|*}"
            local ts="${rest#*|}"
            fail "$label: last bar has NULL indicator field(s): $fields  (ts=$ts)"
            ;;
        FAIL_EMPTY)
            warn "$label: endpoint returned zero bars"
            ;;
        FAIL_HTTP\|*)
            local code="${result#FAIL_HTTP|}"
            fail "$label: endpoint returned HTTP $code"
            ;;
        FAIL_PARSE)
            fail "$label: response is not valid JSON"
            ;;
        *)
            fail "$label: unrecognised check result ($result)"
            ;;
    esac
}

# Ticker chart
TICKER_URL="http://localhost:${SERVICE_PORT}/v1/signals/${SAMPLE_TICKER_EX}/${SAMPLE_TICKER_SYM}/history?years=3"
info "fetching ticker:   $TICKER_URL"
TICKER_RESULT="$(_chart_check_last_bar "$TICKER_URL")"
_present_chart_result "ticker $SAMPLE_TICKER_EX/$SAMPLE_TICKER_SYM" "$TICKER_RESULT"

# Industry chart — URL-encode the name (spaces, ampersands)
INDUSTRY_ENC="$(printf '%s' "$SAMPLE_INDUSTRY" | python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')"
INDUSTRY_URL="http://localhost:${SERVICE_PORT}/v1/signals/industry/${INDUSTRY_ENC}/history?years=3"
info "fetching industry: $INDUSTRY_URL"
INDUSTRY_RESULT="$(_chart_check_last_bar "$INDUSTRY_URL")"
_present_chart_result "industry '$SAMPLE_INDUSTRY'" "$INDUSTRY_RESULT"

# ===== 7. Summary =====
section "Summary"

TOTAL_CHECKS=$((PASS_COUNT + WARN_COUNT + FAIL_COUNT))
say "  ${GREEN}${PASS_COUNT} pass${RESET}, ${YELLOW}${WARN_COUNT} warn${RESET}, ${RED}${FAIL_COUNT} fail${RESET}  out of $TOTAL_CHECKS checks"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    say ""
    say "${RED}${BOLD}FAIL — reasons:${RESET}"
    for r in "${FAIL_REASONS[@]}"; do
        say "  - $r"
    done
    [[ $QUIET -eq 1 ]] && echo "FAIL: $FAIL_COUNT check(s) failed: ${FAIL_REASONS[*]}"
    exit 1
elif [[ "$WARN_COUNT" -gt 0 ]]; then
    [[ $QUIET -eq 1 ]] && echo "WARN: $WARN_COUNT check(s) warned, $PASS_COUNT pass"
    exit 0
else
    say "\n  ${GREEN}${BOLD}All checks passed.${RESET}"
    [[ $QUIET -eq 1 ]] && echo "PASS: all $PASS_COUNT checks ok"
    exit 0
fi