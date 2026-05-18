# Running the service

A trip_ema deployment is three to four separate processes. They can
run on the same host (development) or different hosts (production),
as long as they all see the same Postgres and Redis.

## Initial setup

```bash
# 1. Install
pip install trip-ema[all,migrations]

# 2. Set up the database
export TRIP_EMA_DB_PASSWORD=...
alembic -x config=/path/to/config.yaml upgrade head

# 3. (Optional) Set up the per-role users — Phase 12
# Run as a Postgres superuser:
psql -c "ALTER USER trip_etl_user WITH LOGIN PASSWORD 'etl_secret';"
psql -c "ALTER USER trip_engine_user WITH LOGIN PASSWORD 'eng_secret';"
psql -c "ALTER USER trip_gui_user WITH LOGIN PASSWORD 'gui_secret';"
# Then add the `roles:` block to config.yaml.

# 4. Seed initial ticker universe
TRIP_EMA_DB_PASSWORD=... trip-ema-etl refresh-listings --config config.yaml
TRIP_EMA_DB_PASSWORD=... trip-ema-etl backfill --years 3 --config config.yaml
```

## Running the processes

Each runs in its own process / systemd unit / Docker container:

```bash
# Main service (engine + HTTP API)
TRIP_EMA_DB_PASSWORD=... trip-ema --config config.yaml

# Notifier worker
TRIP_EMA_DB_PASSWORD=... TRIP_EMA_REDIS_PASSWORD=... \
    trip-ema-notifier --config config.yaml

# ETL — run on a schedule (cron / systemd timer)
TRIP_EMA_DB_PASSWORD=... trip-ema-etl incremental-eod --config config.yaml

# Operator console — on each operator's workstation
trip-ema-console --backend-url https://trip-ema.internal --token $ADMIN_TOKEN
```

The console can also be distributed as a standalone binary built
via PyInstaller — see `gui_installer.spec` in the repo root and
the `build-gui-installer` job in `.github/workflows/release.yml`.

## Restart safety

All processes are restart-safe:

- `trip-ema` rebuilds the `SignalCache` from raw bars on startup
  (prime_all_tickers). Restart cost is proportional to the ticker
  count × history depth; for a typical NSE universe it's seconds.
- `trip-ema-etl` is idempotent — re-running `refresh-listings` or
  `incremental-eod` doesn't double-insert.
- `trip-ema-notifier` resumes from the Redis queue; in-flight jobs
  are protected by the dedup hash (failed mid-send → next worker
  re-tries; or the operator replays from the DLQ).
- `trip-ema-console` holds no persistent state — it's a thin HTTP
  client.
