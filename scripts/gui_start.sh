#!/usr/bin/env bash
#
# Developer desktop startup: ensure the operator console GUI is
# running. Idempotent — re-running while the GUI is open is a no-op.
# Not for production servers.
#
# Usage:
#   ./scripts/gui_start.sh
#   ./scripts/gui_start.sh /path/to/other_config.yaml

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CONFIG="${1:-config.yaml}"

echo "=== Ensuring GUI is up ==="
if pgrep -f "trip.ema.console" > /dev/null; then
    echo "  GUI already running, leaving it alone"
    exit 0
fi

# Check we actually have a display before trying
if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    echo "  FAIL: no DISPLAY env var. GUI needs a desktop session."
    echo "  (If running over SSH, use ssh -X or run from a terminal"
    echo "  on the machine with the display.)"
    exit 1
fi

nohup "$REPO_ROOT/.venv/bin/trip-ema-console" --config "$CONFIG" > /tmp/trip-ema-console.log 2>&1 &
echo "  GUI started, PID $!"
echo "  Logs: tail -f /tmp/trip-ema-console.log"