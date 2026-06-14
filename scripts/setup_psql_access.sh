#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# setup_psql_access.sh
#
# Idempotent setup so the invoking Linux user can run
#     psql -U trip_etl_user -d trip_ema -c "..."
# (and the same for trip_engine_user, trip_gui_user) without sudo, password
# prompts, or -h flags.
#
# Run with:    ./setup_psql_access.sh
# (auto-elevates via sudo, preserving TRIP_EMA_DB_PASSWORD from your env)
# -----------------------------------------------------------------------------

set -euo pipefail

# ----- 1. Auto-elevate, preserving TRIP_EMA_DB_PASSWORD -----------------------
if [[ $EUID -ne 0 ]]; then
    if [[ -z "${TRIP_EMA_DB_PASSWORD:-}" ]]; then
        echo "ERROR: TRIP_EMA_DB_PASSWORD is not set in this shell." >&2
        echo "       export TRIP_EMA_DB_PASSWORD='...' and re-run." >&2
        exit 1
    fi
    echo "[*] Re-running under sudo (preserving TRIP_EMA_DB_PASSWORD)..."
    exec sudo --preserve-env=TRIP_EMA_DB_PASSWORD bash "$0" "$@"
fi

# ----- 2. Resolve the invoking user -------------------------------------------
INVOKING_USER="${SUDO_USER:-}"
if [[ -z "$INVOKING_USER" || "$INVOKING_USER" == "root" ]]; then
    echo "ERROR: this script must be invoked via 'sudo' from a regular user," \
         "not as root directly, because it writes ~/.pgpass for that user." >&2
    exit 1
fi
INVOKING_HOME="$(getent passwd "$INVOKING_USER" | cut -d: -f6)"
if [[ -z "$INVOKING_HOME" || ! -d "$INVOKING_HOME" ]]; then
    echo "ERROR: cannot resolve home directory for user $INVOKING_USER" >&2
    exit 1
fi
echo "[*] Setting up psql access for user: $INVOKING_USER (home: $INVOKING_HOME)"

# ----- 3. Sanity checks -------------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
    echo "ERROR: psql not found in PATH" >&2; exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not found; this script assumes systemd" >&2; exit 1
fi
if [[ -z "${TRIP_EMA_DB_PASSWORD:-}" ]]; then
    echo "ERROR: TRIP_EMA_DB_PASSWORD did not survive the sudo elevation." >&2
    echo "       Make sure sudo's env_keep includes it, or set it before running." >&2
    exit 1
fi
echo "[*] TRIP_EMA_DB_PASSWORD is set (length: ${#TRIP_EMA_DB_PASSWORD})"

# ----- 4. Prompt for the postgres ROLE password ONCE --------------------------
echo
echo "Enter the password for the *postgres* PostgreSQL role"
echo "(this is the DB superuser password, NOT your sudo password,"
echo " and NOT TRIP_EMA_DB_PASSWORD):"
read -r -s POSTGRES_PW
echo
if [[ -z "$POSTGRES_PW" ]]; then
    echo "ERROR: empty password for postgres role" >&2; exit 1
fi

# Helper: run psql as the postgres role using PGPASSWORD (no interactive prompt).
pgq() {
    local sql="$1"; shift || true
    sudo -u postgres PGPASSWORD="$POSTGRES_PW" psql -v ON_ERROR_STOP=1 -tAc "$sql" "$@"
}

if ! pgq "SELECT 1;" >/dev/null 2>&1; then
    echo "ERROR: cannot authenticate as the postgres role with the given password." >&2
    exit 1
fi
echo "[*] postgres-role authentication OK; will reuse for all subsequent psql calls"

# ----- 5. Detect PG version + pg_hba.conf path --------------------------------
HBA_FILE="$(pgq 'SHOW hba_file;' 2>/dev/null || true)"
if [[ -z "$HBA_FILE" || ! -f "$HBA_FILE" ]]; then
    echo "ERROR: could not determine pg_hba.conf path; is postgres running?" >&2
    exit 1
fi
PG_MAJOR="$(echo "$HBA_FILE" | sed -nE 's#.*/postgresql/([0-9]+)/.*#\1#p')"
if [[ -z "$PG_MAJOR" ]]; then
    PG_MAJOR="$(pgq 'SHOW server_version_num;' | awk '{print int($1/10000)}')"
fi
echo "[*] PostgreSQL major version: $PG_MAJOR"
echo "[*] pg_hba.conf: $HBA_FILE"

# ----- 6. Ensure target database exists --------------------------------------
DB_EXISTS="$(pgq "SELECT 1 FROM pg_database WHERE datname='trip_ema';" || true)"
if [[ "$DB_EXISTS" != "1" ]]; then
    echo "ERROR: database 'trip_ema' does not exist. Run alembic upgrade head first." >&2
    exit 1
fi
echo "[*] Database 'trip_ema' exists"

# ----- 7. Ensure the three application roles exist with the right password ----
ROLES=(trip_etl_user trip_engine_user trip_gui_user)
ESCAPED_PW="${TRIP_EMA_DB_PASSWORD//\'/\'\'}"
for ROLE in "${ROLES[@]}"; do
    EXISTS="$(pgq "SELECT 1 FROM pg_roles WHERE rolname='${ROLE}';" || true)"
    if [[ "$EXISTS" != "1" ]]; then
        echo "[+] Creating role: $ROLE"
        pgq "CREATE ROLE ${ROLE} LOGIN PASSWORD '${ESCAPED_PW}';" >/dev/null
    else
        echo "[=] Role exists: $ROLE  (resetting password to match env var)"
        pgq "ALTER ROLE ${ROLE} WITH LOGIN PASSWORD '${ESCAPED_PW}';" >/dev/null
    fi
done

# ----- 8. Patch pg_hba.conf if needed ----------------------------------------
MARKER_BEGIN="# >>> trip_ema setup_psql_access.sh BEGIN (do not edit) >>>"
MARKER_END="# <<< trip_ema setup_psql_access.sh END <<<"

if grep -qF "$MARKER_BEGIN" "$HBA_FILE"; then
    echo "[=] pg_hba.conf already contains trip_ema block; leaving as-is"
    HBA_CHANGED=0
else
    echo "[+] Adding trip_ema local + host rules to pg_hba.conf"
    BACKUP="${HBA_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
    cp -p "$HBA_FILE" "$BACKUP"
    echo "    backup written: $BACKUP"

    BLOCK="$(cat <<EOF
${MARKER_BEGIN}
local   trip_ema   trip_etl_user                          scram-sha-256
local   trip_ema   trip_engine_user                       scram-sha-256
local   trip_ema   trip_gui_user                          scram-sha-256
host    trip_ema   trip_etl_user      127.0.0.1/32        scram-sha-256
host    trip_ema   trip_engine_user   127.0.0.1/32        scram-sha-256
host    trip_ema   trip_gui_user      127.0.0.1/32        scram-sha-256
${MARKER_END}
EOF
)"

    ANCHOR_LINE="$(grep -nE '^[[:space:]]*(local|host)[[:space:]]' "$HBA_FILE" | head -1 | cut -d: -f1 || true)"
    if [[ -n "$ANCHOR_LINE" ]]; then
        TMP="$(mktemp)"
        awk -v ln="$ANCHOR_LINE" -v blk="$BLOCK" '
            NR == ln { print blk }
            { print }
        ' "$HBA_FILE" > "$TMP"
        cat "$TMP" > "$HBA_FILE"
        rm -f "$TMP"
    else
        printf '\n%s\n' "$BLOCK" >> "$HBA_FILE"
    fi
    HBA_CHANGED=1
fi

# ----- 9. Reload PostgreSQL if pg_hba.conf changed ---------------------------
if [[ "${HBA_CHANGED:-0}" == "1" ]]; then
    echo "[*] Reloading PostgreSQL service"
    systemctl reload "postgresql@${PG_MAJOR}-main" 2>/dev/null \
        || systemctl reload postgresql
fi

# ----- 10. Write/refresh ~/.pgpass for the invoking user ----------------------
# IMPORTANT: .pgpass uses first-match-wins. Any stale lines for our roles ABOVE
# the marker block will silently shadow the correct ones. So we strip ALL
# existing entries for our three roles BEFORE writing the marker block.
PGPASS="${INVOKING_HOME}/.pgpass"
PGPASS_BLOCK_BEGIN="# >>> trip_ema setup_psql_access.sh BEGIN >>>"
PGPASS_BLOCK_END="# <<< trip_ema setup_psql_access.sh END <<<"

read -r -d '' DESIRED_BLOCK <<EOF || true
${PGPASS_BLOCK_BEGIN}
*:*:trip_ema:trip_etl_user:${TRIP_EMA_DB_PASSWORD}
*:*:trip_ema:trip_engine_user:${TRIP_EMA_DB_PASSWORD}
*:*:trip_ema:trip_gui_user:${TRIP_EMA_DB_PASSWORD}
${PGPASS_BLOCK_END}
EOF

# Build a sanitized version: strip our marker block (if present) and ALL
# legacy lines matching our three roles. Then re-append a fresh marker block.
if [[ -f "$PGPASS" ]]; then
    TMP="$(mktemp)"
    awk -v b="$PGPASS_BLOCK_BEGIN" -v e="$PGPASS_BLOCK_END" '
        $0 == b { skip = 1; next }
        $0 == e { skip = 0; next }
        skip { next }
        # Drop any stale entry that targets one of our three roles, regardless
        # of how it was written. Matches the literal role name as the 4th field
        # of a pgpass record (host:port:db:user:pw).
        /^[^#].*:trip_ema:(trip_etl_user|trip_engine_user|trip_gui_user):/ { next }
        { print }
    ' "$PGPASS" > "$TMP"
    cat "$TMP" > "$PGPASS"
    rm -f "$TMP"
fi

# Now append a fresh, authoritative block.
{
    if [[ -f "$PGPASS" && -s "$PGPASS" ]] && [[ "$(tail -c 1 "$PGPASS")" != $'\n' ]]; then
        echo ""
    fi
    echo "$DESIRED_BLOCK"
} >> "$PGPASS"

chown "$INVOKING_USER:$INVOKING_USER" "$PGPASS"
chmod 600 "$PGPASS"
echo "[*] ~/.pgpass written (mode 600, owner $INVOKING_USER, stale lines pruned)"

# ----- 11. Smoke test ---------------------------------------------------------
echo
echo "[*] Smoke test: connecting as trip_etl_user via socket (no password prompt expected)"
if sudo -u "$INVOKING_USER" psql -U trip_etl_user -d trip_ema -tAc "SELECT 'OK: ' || current_user || '@' || current_database();" ; then
    echo "[*] Success."
else
    echo "[!] Smoke test FAILED. Inspect $HBA_FILE and check that systemctl reload succeeded." >&2
    exit 1
fi

unset POSTGRES_PW
unset TRIP_EMA_DB_PASSWORD

echo
echo "=============================================================="
echo "Done. You can now run, as $INVOKING_USER, without sudo:"
echo "  psql -U trip_etl_user    -d trip_ema -c \"SELECT 1;\""
echo "  psql -U trip_engine_user -d trip_ema -c \"SELECT 1;\""
echo "  psql -U trip_gui_user    -d trip_ema -c \"SELECT 1;\""
echo "=============================================================="
