#!/usr/bin/env bash
#
# Restart the trip-ema service cleanly.
#
# Workflow:
#   1. Find any running trip-ema instance bound to SERVICE_PORT.
#      Send SIGTERM. Poll for graceful shutdown up to
#      SHUTDOWN_WAIT_SECONDS, escalating to SIGKILL if it overruns.
#   2. Verify the port is free.
#   3. Start the service in the background via nohup so it survives
#      this script's exit.
#   4. Poll /healthz for up to PRIME_WAIT_SECONDS to confirm priming.
#   5. Print the /healthz response so you see cache_size before the
#      script returns.
#   6. Exit, leaving the service running.
#
# Usage:
#   With the env var set in your shell:
#     export TRIP_EMA_DB_PASSWORD=devpassword
#     ./scripts/restart_service.sh
#
#   Or inline (one-shot):
#     TRIP_EMA_DB_PASSWORD=devpassword ./scripts/restart_service.sh
#
#   With a custom config:
#     TRIP_EMA_DB_PASSWORD=devpassword ./scripts/restart_service.sh /path/to/config.yaml
#
# Prereqs:
#   - TRIP_EMA_DB_PASSWORD must be set (or in ~/.pgpass equivalent
#     that the config consumes).
#   - `trip-ema` must be on PATH (from the venv).
#
# Logs go to /tmp/trip-ema.log (overwritten each restart).

set -euo pipefail

# Default password for the dev environment. Override by exporting
# TRIP_EMA_DB_PASSWORD before running, or by passing it inline.
export TRIP_EMA_DB_PASSWORD="${TRIP_EMA_DB_PASSWORD:-devpassword}"

# ===== Configuration =====
SERVICE_PORT=8000
CONFIG="${1:-config.yaml}"
LOG_FILE=/tmp/trip-ema.log
PRIME_WAIT_SECONDS=120
SHUTDOWN_WAIT_SECONDS=30   # trip-ema's graceful shutdown stops
                           # SubscriberDistributor + OrchestratorPool
                           # (one thread per market) + ComputeScheduler
                           # + uvicorn + disposes the DB engine. Under
                           # load this routinely takes 5-15s; 30s gives
                           # comfortable headroom before SIGKILL.
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

# Step 1 — find and stop whatever's bound to the port.
echo "=== Step 1: stopping existing trip-ema ==="

# `|| true` absorbs grep's exit-1-on-no-match (the common case when
# no service is running). Without it, set -euo pipefail kills this
# script silently here, BEFORE the [[ -z ]] branch below can handle
# the "nothing to kill" case.
existing_pids=$(ss -tlnp 2>/dev/null \
    | grep -E ":${SERVICE_PORT}\s" \
    | grep -oE 'pid=[0-9]+' \
    | cut -d= -f2 \
    | sort -u || true)

if [[ -z "$existing_pids" ]]; then
    echo "  no process found on port ${SERVICE_PORT}"
else
    for pid in $existing_pids; do
        # Confirm the process is actually trip-ema before killing it.
        # ss reports any process bound to the port, including ones
        # that aren't ours. Match by command line to be safe.
        cmd=$(ps -p "$pid" -o comm= 2>/dev/null || echo "")
        if [[ "$cmd" == "trip-ema" ]] || [[ "$cmd" == "python"* ]]; then
            echo "  sending SIGTERM to PID $pid (cmd: $cmd)"
            kill "$pid" 2>/dev/null || true
        else
            echo "  PID $pid is '$cmd', not trip-ema — refusing to kill"
            echo "  resolve manually before re-running"
            exit 1
        fi
    done

    # Poll for graceful shutdown. The old script used a fixed
    # `sleep 3` here, which raced trip-ema's actual shutdown
    # (typically 5-15s) and caused Step 2's port check to fail —
    # leaving the operator with a killed-but-not-restarted service.
    echo "  waiting up to ${SHUTDOWN_WAIT_SECONDS}s for graceful shutdown..."
    shutdown_ok=0
    for i in $(seq 1 "$SHUTDOWN_WAIT_SECONDS"); do
        all_dead=1
        for pid in $existing_pids; do
            if kill -0 "$pid" 2>/dev/null; then
                all_dead=0
                break
            fi
        done

        port_free=1
        if ss -tlnp 2>/dev/null | grep -q ":${SERVICE_PORT}\s"; then
            port_free=0
        fi

        if [[ $all_dead -eq 1 && $port_free -eq 1 ]]; then
            echo "  shutdown complete after ${i}s"
            shutdown_ok=1
            break
        fi
        sleep 1
    done

    # Escalate if graceful shutdown overran.
    if [[ $shutdown_ok -eq 0 ]]; then
        echo "  graceful shutdown exceeded ${SHUTDOWN_WAIT_SECONDS}s — escalating to SIGKILL"
        for pid in $existing_pids; do
            kill -9 "$pid" 2>/dev/null || true
        done
        sleep 2
    fi
fi

# Step 2 — final port check (safety net; usually passes after Step 1).
# Even after SIGKILL, a socket can sit in TIME_WAIT briefly. Don't
# escalate from here — just report and bail so the operator can
# investigate.
echo ""
echo "=== Step 2: verifying port ${SERVICE_PORT} is free ==="
if ss -tlnp 2>/dev/null | grep -q ":${SERVICE_PORT}\s"; then
    echo "FAIL: port ${SERVICE_PORT} is still bound after Step 1."
    echo "Check manually with: ss -tlnp | grep :${SERVICE_PORT}"
    exit 1
fi
echo "  port is free"

# Step 3 — start the service in the background.
echo ""
echo "=== Step 3: starting trip-ema (config=${CONFIG}) ==="
# nohup detaches from the controlling terminal so closing the shell
# doesn't kill the service. `setsid` would do the same; nohup is
# more portable.
nohup "$REPO_ROOT/.venv/bin/trip-ema" --config "$CONFIG" > "$LOG_FILE" 2>&1 &
SERVICE_PID=$!
echo "  started, PID $SERVICE_PID"
echo "  logs at $LOG_FILE"

# Step 4 — wait for priming to complete.
echo ""
echo "=== Step 4: waiting up to ${PRIME_WAIT_SECONDS}s for priming ==="
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

# Step 5 — publish the cache state.
echo ""
echo "=== Step 5: cache state ==="
response=$(curl -fsS "$HEALTH_URL")
echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"

# Step 6 — exit cleanly, leaving the service running.
echo ""
echo "=== Service restarted successfully ==="
echo "PID:     $SERVICE_PID"
echo "Port:    $SERVICE_PORT"
echo "Logs:    tail -f $LOG_FILE"
echo "Stop:    kill $SERVICE_PID  (or re-run this script)"
exit 0
