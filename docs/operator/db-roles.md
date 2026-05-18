# DB role separation

Phase 12 added three Postgres roles for defense-in-depth against
cross-process write bugs. The full story is in
[ARCHITECTURE.md §8.6](../architecture/operations.md); this page
covers the operator workflow.

## The three roles

- **`trip_etl_user`** — WRITE on `tickers`, `ohlcv_bars`,
  `etl_run_log`, `parked_tickers`; no access to signals or
  subscribers
- **`trip_engine_user`** — READ bars; WRITE signals, subscribers,
  notification_log
- **`trip_gui_user`** — READ-only across all public-schema tables

Roles are created by Alembic migration `0002_db_roles.py` with
`NOLOGIN` and no password. Operators grant `LOGIN` and set
passwords as a deployment step.

## Prerequisites

The migration user (the one in `config.yaml` running
`alembic upgrade head`) needs the `CREATEROLE` Postgres attribute
to execute the `CREATE ROLE` statements. Without it, migration
0002 will fail with a `permission denied` error and the database
will be left at revision `0001_initial_schema`.

Grant it as a Postgres superuser before running migrations:

```sql
ALTER ROLE trip_ema_app CREATEROLE;
```

This is a one-time setup step. If you'd rather not use role
separation at all, you can stop reading here — the application
works correctly with whatever user is in `config.yaml`, and the
`roles:` block in the config file is entirely optional.

## Setup

```sql
-- Run as a Postgres superuser
ALTER USER trip_etl_user    WITH LOGIN PASSWORD 'etl_secret';
ALTER USER trip_engine_user WITH LOGIN PASSWORD 'engine_secret';
ALTER USER trip_gui_user    WITH LOGIN PASSWORD 'gui_secret';

-- Verify privileges
\du trip_etl_user
\du trip_engine_user
\du trip_gui_user
```

Then in `config.yaml`:

```yaml
database:
  # Top-level user — used for migrations (which need ALTER
  # privileges the per-role users don't have) and as a fallback
  # for any role not explicitly configured.
  host: db.example.com
  database: trip_ema
  user: trip_ema_app
  password_env: TRIP_EMA_DB_PASSWORD

  roles:
    etl:
      user: trip_etl_user
      password_env: TRIP_EMA_DB_ETL_PASSWORD
    engine:
      user: trip_engine_user
      password_env: TRIP_EMA_DB_ENGINE_PASSWORD
    gui:
      user: trip_gui_user
      password_env: TRIP_EMA_DB_GUI_PASSWORD
```

And in the deployment environment, set all the env vars:

```bash
export TRIP_EMA_DB_PASSWORD=...
export TRIP_EMA_DB_ETL_PASSWORD=...
export TRIP_EMA_DB_ENGINE_PASSWORD=...
export TRIP_EMA_DB_GUI_PASSWORD=...
```

## Backward compatibility

Adoption is opt-in. Without a `database.roles:` section, every
process uses the top-level user/password — preserving existing
deployments unchanged.

You can also enable roles incrementally. For example, enable only
the `gui` role first (the safest, since it's read-only), confirm
the operator console still works, then enable `etl` and finally
`engine`. Each role independently falls back to the top-level
credentials when not configured.

## Verification

After setup, verify role enforcement by attempting a forbidden
operation:

```bash
psql -U trip_gui_user -d trip_ema -c \
    "INSERT INTO ohlcv_bars (ticker_id, ...) VALUES (...);"
# Expected: ERROR: permission denied for table ohlcv_bars
```

If the insert *succeeds* against the GUI role, the GRANT model
is broken — investigate before proceeding to production.
