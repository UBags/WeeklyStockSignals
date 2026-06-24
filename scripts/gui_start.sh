#!/usr/bin/env bash
#
# Developer desktop startup: ensure the operator console GUI is
# running. Idempotent by default — re-running while the GUI is open
# is a no-op. Use --force to kill any existing instance and restart.
#
# Not for production servers.
#
# Usage:
#   ./scripts/gui_start.sh
#   ./scripts/gui_start.sh /path/to/other_config.yaml
#   ./scripts/gui_start.sh --force
#   ./scripts/gui_start.sh --force /path/to/other_config.yaml
#
# Flags:
#   --force     Kill any existing GUI process(es) and start a fresh one.
#               Useful after env-var changes (e.g. TRIP_EMA_ADMIN_TOKEN
#               regenerated) — the running GUI needs a relaunch to pick
#               up the new token from the parent shell's environment.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ===== Parse args =====
FORCE=0
CONFIG="config.yaml"
for arg in "$@"; do
    case "$arg" in
        --force|-f)
            FORCE=1
            ;;
        -h|--help)
            sed -n '/^# Usage:/,/^# Flags:/p; /^# Flags:/,/^$/p' "$0" \
                | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            CONFIG="$arg"
            ;;
    esac
done
# ======================

LOG_FILE=/tmp/trip-ema-console.log
# pgrep pattern: match the venv binary or python invocation. Using `-f`
# (full command line) lets us catch both `trip-ema-console` and any
# direct `python -m trip_ema.gui` invocation.
PGREP_PATTERN="trip.ema.console|trip_ema\.gui"

# Helper: find existing GUI PIDs (returns empty string if none)
find_existing_gui_pids() {
    pgrep -f "$PGREP_PATTERN" 2>/dev/null || true
}

echo "=== Ensuring GUI is up ==="

existing_pids=$(find_existing_gui_pids)

if [[ -n "$existing_pids" ]]; then
    if [[ $FORCE -eq 1 ]]; then
        # --force: kill the existing instance(s), then continue to launch.
        # SIGTERM first so the Qt event loop has a chance to close windows
        # and persist any session state; escalate to SIGKILL only if the
        # process refuses to die within a short window.
        echo "  --force: stopping existing GUI process(es): $(echo "$existing_pids" | tr '\n' ' ')"
        for pid in $existing_pids; do
            kill "$pid" 2>/dev/null || true
        done

        # Poll up to 10s for graceful shutdown. The GUI usually closes in
        # 1-3s; 10s is comfortable headroom.
        gone=0
        for i in $(seq 1 10); do
            if [[ -z "$(find_existing_gui_pids)" ]]; then
                echo "  shutdown complete after ${i}s"
                gone=1
                break
            fi
            sleep 1
        done

        if [[ $gone -eq 0 ]]; then
            stubborn=$(find_existing_gui_pids)
            echo "  graceful shutdown overran 10s — escalating to SIGKILL: $(echo "$stubborn" | tr '\n' ' ')"
            for pid in $stubborn; do
                kill -9 "$pid" 2>/dev/null || true
            done
            sleep 1
        fi

        # Final check before launch — if something is still bound, bail.
        # Don't try a third escalation; let the operator investigate.
        if [[ -n "$(find_existing_gui_pids)" ]]; then
            echo "  FAIL: GUI process(es) still present after SIGKILL:"
            find_existing_gui_pids
            echo "  Resolve manually and re-run."
            exit 1
        fi
    else
        # No --force: respect the running GUI and exit clean.
        echo "  GUI already running (PIDs: $(echo "$existing_pids" | tr '\n' ' '))"
        echo "  Pass --force to kill it and restart with current env."
        exit 0
    fi
fi

# Check we actually have a display before trying
if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    echo "  FAIL: no DISPLAY env var. GUI needs a desktop session."
    echo "  (If running over SSH, use ssh -X or run from a terminal"
    echo "  on the machine with the display.)"
    exit 1
fi

# Warn (don't block) if TRIP_EMA_ADMIN_TOKEN isn't set — the GUI's
# admin-gated tabs (Subscribers, System Health detail) will get 403s.
if [[ -z "${TRIP_EMA_ADMIN_TOKEN:-}" ]]; then
    echo "  WARN: TRIP_EMA_ADMIN_TOKEN not set — admin tabs will return 403"
    echo "        Source ~/.bashrc or set it in this shell before re-running."
fi

nohup "$REPO_ROOT/.venv/bin/trip-ema-console" --config "$CONFIG" > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "  GUI started, PID $NEW_PID"
echo "  Logs: tail -f $LOG_FILE"