#!/usr/bin/env bash
#
# Restart the trip-ema service cleanly.
#
# Workflow:
#   0. Verify env (TRIP_EMA_DB_PASSWORD + TRIP_EMA_ADMIN_TOKEN). Auto-
#      source ~/.bashrc if the env vars are missing in this shell.
#      Test the DB password BEFORE starting the service so a bad
#      password fails diagnostically here instead of buried inside a
#      SQLAlchemy traceback.
#   1. Find any running trip-ema instance bound to SERVICE_PORT. Send
#      SIGTERM. Poll for graceful shutdown up to SHUTDOWN_WAIT_SECONDS,
#      escalating to SIGKILL if it overruns.
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
#   - TRIP_EMA_DB_PASSWORD must be set, either in the calling shell or
#     persisted to ~/.bashrc as `export TRIP_EMA_DB_PASSWORD=...`. There
#     is intentionally NO password default — the script fails loud if
#     the env var is missing rather than connecting with a fake password.
#   - TRIP_EMA_ADMIN_TOKEN should be set for admin endpoints to be
#     accessible. The service starts without it, but admin routes
#     return 503.
#   - `trip-ema` must be on PATH (from the venv).
#
# Logs go to /tmp/trip-ema.log (overwritten each restart).

set -euo pipefail

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

# ============================================================================
# Step 0 — env diagnostics
# ============================================================================
# The single biggest source of "service died on startup" pain has been the
# env var TRIP_EMA_DB_PASSWORD getting lost (killed parent shell, opened a
# new terminal that didn't source ~/.bashrc, etc.). The old script had a
# `:-devpassword` fallback that masked this — the service started with a
# garbage password and died inside SQLAlchemy. This block fails LOUD before
# we touch the service.

echo "=== Step 0: env diagnostics ==="

# 0a — if TRIP_EMA_DB_PASSWORD is empty, try to source ~/.bashrc to recover
# it. Most users persist their exports there but forget to source a fresh
# shell. This makes the script idempotent across shell-lifecycle hiccups.
if [[ -z "${TRIP_EMA_DB_PASSWORD:-}" ]] && [[ -f "$HOME/.bashrc" ]]; then
    echo "  TRIP_EMA_DB_PASSWORD empty in current shell — sourcing ~/.bashrc"
    # shellcheck disable=SC1090
    set +u   # ~/.bashrc may reference unset vars; don't crash on those
    source "$HOME/.bashrc" 2>/dev/null || true
    set -u
fi

if [[ -z "${TRIP_EMA_DB_PASSWORD:-}" ]]; then
    cat >&2 <<'EOF'
FAIL: TRIP_EMA_DB_PASSWORD is not set in this shell, and sourcing
      ~/.bashrc did not provide it. There is intentionally NO default
      (a wrong-password fallback hid this exact failure earlier today).

Resolution:
  1. If you know the password, export it now:
         export TRIP_EMA_DB_PASSWORD='<password>'
     and persist it in ~/.bashrc for future shells:
         echo "export TRIP_EMA_DB_PASSWORD='<password>'" >> ~/.bashrc

  2. If the password has been lost (killed parent shell with no
     persistence), reset the Postgres role:
         NEW=$(openssl rand -hex 16)
         sudo -u postgres psql -c "ALTER ROLE trip_ema_app WITH LOGIN PASSWORD '$NEW';"
         export TRIP_EMA_DB_PASSWORD="$NEW"
         echo "export TRIP_EMA_DB_PASSWORD='$NEW'" >> ~/.bashrc

  3. Then re-run this script.
EOF
    exit 1
fi
echo "  TRIP_EMA_DB_PASSWORD set (length ${#TRIP_EMA_DB_PASSWORD})"

# 0b — admin token: warn only, don't block
if [[ -z "${TRIP_EMA_ADMIN_TOKEN:-}" ]]; then
    echo "  WARN: TRIP_EMA_ADMIN_TOKEN is not set — admin endpoints will return 503"
    echo "        (the service will start, but the GUI admin tab + /v1/admin/* are gated)"
else
    echo "  TRIP_EMA_ADMIN_TOKEN set (${TRIP_EMA_ADMIN_TOKEN:0:8}...)"
fi

# 0c — verify the DB password works BEFORE starting the service.
# We use psql with PGPASSWORD over TCP loopback. If this fails, the
# service will also fail in the exact same way, but it's much harder
# to diagnose from inside a 50-line stack trace.
#
# Config-driven: we read host, port, user, database from config.yaml via
# python so this script doesn't have to duplicate config parsing.
echo "  testing DB connectivity..."
DB_CHECK=$(python3 - <<'PY' 2>&1
import os, sys, yaml
try:
    cfg = yaml.safe_load(open("config.yaml"))["database"]
    host = cfg.get("host", "localhost")
    port = cfg.get("port", 5432)
    user = cfg["user"]
    dbname = cfg["database"]
    print(f"{host}|{port}|{user}|{dbname}")
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PY
)
if [[ "${DB_CHECK}" == ERROR:* ]] || [[ -z "${DB_CHECK}" ]]; then
    echo "  WARN: could not parse config.yaml database block — skipping DB pre-check"
    echo "        (${DB_CHECK})"
else
    IFS='|' read -r DB_HOST DB_PORT DB_USER DB_NAME <<< "$DB_CHECK"
    if command -v psql >/dev/null 2>&1; then
        if PGPASSWORD="$TRIP_EMA_DB_PASSWORD" psql \
            -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
            -tAc "SELECT 1" >/dev/null 2>&1; then
            echo "  DB connectivity OK (${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME})"
        else
            cat >&2 <<EOF
FAIL: cannot connect to Postgres as ${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME}
      with the password currently in TRIP_EMA_DB_PASSWORD.

This is the same error that would have killed the service ~10 seconds
into startup. Failing early with a clear message saves you scrolling
through a SQLAlchemy stack trace.

Likely causes:
  - The password in your env doesn't match the trip_ema_app role.
    Reset with: sudo -u postgres psql -c "ALTER ROLE trip_ema_app \\
                  WITH LOGIN PASSWORD '\$TRIP_EMA_DB_PASSWORD';"
  - Postgres is not running. Check: sudo systemctl status postgresql
  - pg_hba.conf doesn't allow host TCP auth for this user. Inspect
    the file at: sudo -u postgres psql -tAc "SHOW hba_file;"
EOF
            exit 1
        fi
    else
        echo "  WARN: psql not on PATH — skipping DB pre-check"
    fi
fi

# Sanity check the config file exists.
if [[ ! -f "$CONFIG" ]]; then
    echo "FAIL: config file not found: $CONFIG"
    exit 1
fi

# ============================================================================
# Step 1 — find and stop whatever's bound to the port.
# ============================================================================
echo ""
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

# ============================================================================
# Step 2 — final port check (safety net; usually passes after Step 1).
# ============================================================================
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

# ============================================================================
# Step 3 — start the service in the background.
# ============================================================================
echo ""
echo "=== Step 3: starting trip-ema (config=${CONFIG}) ==="
# nohup detaches from the controlling terminal so closing the shell
# doesn't kill the service. `setsid` would do the same; nohup is
# more portable.
nohup "$REPO_ROOT/.venv/bin/trip-ema" --config "$CONFIG" > "$LOG_FILE" 2>&1 &
SERVICE_PID=$!
echo "  started, PID $SERVICE_PID"
echo "  logs at $LOG_FILE"

# ============================================================================
# Step 4 — wait for priming to complete.
# ============================================================================
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

# ============================================================================
# Step 5 — publish the cache state.
# ============================================================================
echo ""
echo "=== Step 5: cache state ==="
response=$(curl -fsS "$HEALTH_URL")
echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"

# ============================================================================
# Step 6 — exit cleanly, leaving the service running.
# ============================================================================
echo ""
echo "=== Service restarted successfully ==="
echo "PID:     $SERVICE_PID"
echo "Port:    $SERVICE_PORT"
echo "Logs:    tail -f $LOG_FILE"
echo "Stop:    kill $SERVICE_PID  (or re-run this script)"
exit 0