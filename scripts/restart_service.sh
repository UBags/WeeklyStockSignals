#!/usr/bin/env bash
#
# Restart the trip-ema service cleanly.
#
# Workflow:
#   1. Kill any running trip-ema instance (by port, not by name —
#      `pkill -f trip_ema.app` misses some invocations).
#   2. Sleep 3 seconds to let the OS reclaim the socket.
#   3. Verify port is free.
#   4. Start the service in the background via nohup so it survives
#      this script's exit.
#   5. Sleep 60 seconds to let priming complete.
#   6. Print the /healthz response so you see cache_size before the
#      script returns.
#   7. Exit, leaving the service running.
#
# Usage:
# With the env var set in your shell
#export TRIP_EMA_DB_PASSWORD=devpassword
#./scripts/restart_service.sh
#
# Or inline (one-shot)
#TRIP_EMA_DB_PASSWORD=devpassword ./scripts/restart_service.sh
#
# With a custom config
#TRIP_EMA_DB_PASSWORD=devpassword ./scripts/restart_service.sh /path/to/config.yaml#
#
# Prereqs:
#   - TRIP_EMA_DB_PASSWORD must be set (or in ~/.pgpass equivalent
#     that the config consumes).
#   - `trip-ema` must be on PATH (from the venv).
#
# Logs go to /tmp/trip-ema.log (overwritten each restart).

set -euo pipefail
export TRIP_EMA_DB_PASSWORD=devpassword

# ===== Configuration =====
SERVICE_PORT=8000
CONFIG="${1:-config.yaml}"
LOG_FILE=/tmp/trip-ema.log
PRIME_WAIT_SECONDS=60
# =========================

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HEALTH_URL="http://localhost:${SERVICE_PORT}/healthz"

# Sanity check that the password env var is set. Without it the
# service starts, fails to connect to DB, and exits — we'd notice
# only at the healthz check.
if [[ -z "${TRIP_EMA_DB_PASSWORD:-}" ]]; then
    echo "FAIL: TRIP_EMA_DB_PASSWORD env var not set."
    echo "Set it before running: export TRIP_EMA_DB_PASSWORD='...'"
    exit 1
fi

# Sanity check the config file exists.
if [[ ! -f "$CONFIG" ]]; then
    echo "FAIL: config file not found: $CONFIG"
    exit 1
fi

# Step 1 — find and kill whatever's bound to the port.
echo "=== Step 1: stopping existing trip-ema ==="
existing_pids=$(ss -tlnp 2>/dev/null \
    | grep -E ":${SERVICE_PORT}\s" \
    | grep -oE 'pid=[0-9]+' \
    | cut -d= -f2 \
    | sort -u)

if [[ -z "$existing_pids" ]]; then
    echo "  no process found on port ${SERVICE_PORT}"
else
    for pid in $existing_pids; do
        # Confirm the process is actually trip-ema before killing it.
        # ss reports any process bound to the port, including ones
        # that aren't ours. Match by command line to be safe.
        cmd=$(ps -p "$pid" -o comm= 2>/dev/null || echo "")
        if [[ "$cmd" == "trip-ema" ]] || [[ "$cmd" == "python"* ]]; then
            echo "  killing PID $pid (cmd: $cmd)"
            kill "$pid"
        else
            echo "  PID $pid is '$cmd', not trip-ema — refusing to kill"
            echo "  resolve manually before re-running"
            exit 1
        fi
    done
fi

# Step 2 — wait for the OS to release the socket.
echo ""
echo "=== Step 2: waiting 3s for socket release ==="
sleep 3

# Step 3 — verify port is free.
echo ""
echo "=== Step 3: verifying port ${SERVICE_PORT} is free ==="
if ss -tlnp 2>/dev/null | grep -q ":${SERVICE_PORT}\s"; then
    echo "FAIL: port ${SERVICE_PORT} is still bound after kill+sleep."
    echo "Check manually with: ss -tlnp | grep :${SERVICE_PORT}"
    exit 1
fi
echo "  port is free"

# Step 4 — start the service in the background.
echo ""
echo "=== Step 4: starting trip-ema (config=${CONFIG}) ==="
# nohup detaches from the controlling terminal so closing the shell
# doesn't kill the service. The `setsid` would do the same; nohup
# is more portable.
nohup trip-ema --config "$CONFIG" > "$LOG_FILE" 2>&1 &
SERVICE_PID=$!
echo "  started, PID $SERVICE_PID"
echo "  logs at $LOG_FILE"

# Step 5 — wait for priming to complete.
echo ""
echo "=== Step 5: waiting ${PRIME_WAIT_SECONDS}s for priming ==="
# Poll healthz periodically rather than blindly sleeping. If the
# service dies during startup, fail fast with the log tail.
for i in $(seq 1 "$PRIME_WAIT_SECONDS"); do
    if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
        echo "FAIL: service died during startup. Log tail:"
        tail -30 "$LOG_FILE"
        exit 1
    fi
    if curl -fsS "$HEALTH_URL" > /dev/null 2>&1; then
        echo "  /healthz reachable after ${i}s"
        break
    fi
    sleep 1
    if [[ $i -eq $PRIME_WAIT_SECONDS ]]; then
        echo "FAIL: /healthz not reachable after ${PRIME_WAIT_SECONDS}s. Log tail:"
        tail -30 "$LOG_FILE"
        exit 1
    fi
done

# Step 6 — publish the cache state.
echo ""
echo "=== Step 6: cache state ==="
response=$(curl -fsS "$HEALTH_URL")
echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"

# Step 7 — exit cleanly, leaving the service running.
echo ""
echo "=== Service restarted successfully ==="
echo "PID:     $SERVICE_PID"
echo "Port:    $SERVICE_PORT"
echo "Logs:    tail -f $LOG_FILE"
echo "Stop:    kill $SERVICE_PID  (or re-run this script)"
exit 0