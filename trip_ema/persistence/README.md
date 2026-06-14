# `trip_ema/persistence/` — Postgres repositories + migrator + connection

The data-access layer. Every table has a repository (`*_repo.py`); the
repos are the only modules that issue raw SQL or use SQLAlchemy Core
directly. Higher layers (engine, scheduler, transports) call the repos
through their typed methods.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `connection.py` | `create_engine(config)` (top-level role) and `create_engine_for_role(config, role)` (Phase 12 per-process least-privilege roles) |
| `tables.py` | SQLAlchemy Core `Table()` definitions for every table. Used by repos as the canonical schema reference |
| `migrator.py` | Legacy schema migrator — applies `migrations/legacy/NNN_*.sql` in order. Production startup no longer uses this (Alembic-only since Phase 12); kept for the legacy-parity test fixture |
| `alembic_bridge.py` | (Phase 11+) Bridges legacy-migrator state into Alembic. `stamp_baseline_if_needed()` detects a populated `schema_migrations` table at startup and stamps Alembic's `alembic_version` at `0001_initial_schema` **without** re-running DDL |

## Repositories

| File | Owns | Notable |
|---|---|---|
| `tickers_repo.py` | `tickers` | Universe + tier + parked state |
| `bars_repo.py` | `ohlcv_bars` | `weekly_buckets_with_bars` powers the gap-detection invariant |
| `signals_repo.py` | `signal_events` | Append-only; written by `ComputeScheduler` only |
| `signal_backtest_runs_repo.py` | `signal_backtest_runs` (Phase 15a) | Per-(exchange, ticker, timeframe) resume cursors for the backtest pipeline |
| `subscribers_repo.py` | `subscribers` + filters + `notification_log` | CRUD-heavy; consumed by the GUI's Subscribers tab |
| `exchange_state_repo.py` | `exchange_state` (Phase 14.6) | Per-exchange last-action timestamps; opens + closes `etl_runs` rows |
| `fundamentals_repo.py` | All the fundamentals tables (Phases 1-7) — `instrument_industry`, `instrument_shares`, `quarterly_financials`, `daily_market_cap`, `industry_index`, `industry_index_constituent`, `industry_index_value`, `indicator_state`, `signal_event`, `industry_override`, `fundamentals_run` | Large module; one class per table |

## Connection patterns

- **Top-level role (default).** `create_engine(config)` uses the
  `database.user` + `database.password_env`. Used when Phase 12
  roles are not enabled
- **Per-process role (Phase 12, opt-in).** `create_engine_for_role(config, "etl")`
  uses the etl/engine/gui role's credentials from `database.roles:`.
  ETL processes use `trip_etl_user` (read+write on ETL tables);
  the compute scheduler uses `trip_engine_user` (write on
  `signal_events`, read on `ohlcv_bars`); GUI/HTTP read paths use
  `trip_gui_user` (read-only everything)
- Engine pooling sized for the workload — small (5-10 conns) for
  the main service; per-CLI ephemeral for `trip-ema-etl`

## Repo conventions

- Methods return frozen dataclasses (from `domain/` or
  `fundamentals/__init__.py`), not raw SQLAlchemy Row objects
- Writes return the inserted row's primary key
- Bulk operations use `executemany` / `execute(insert(...).values(...))`
  patterns; not row-by-row loops
- Repos never start transactions internally — callers manage scope
  via `engine.begin()` / `engine.connect()`. Lets callers chain
  multi-repo updates in one transaction

## Tests

`tests/persistence/` — DB-required, uses `pytest-postgresql`. One file
per repo:
- `test_tickers_repo.py`, `test_bars_repo.py`, `test_signals_repo.py`,
  `test_signal_backtest_runs_repo.py`, `test_subscribers_repo.py`,
  `test_exchange_state_repo.py`, `test_fundamentals_repos.py`,
  `test_gui_queries.py`
- `test_migrator.py` — legacy migrator behavior
- `test_legacy_migrator.py` — parity between Alembic + legacy SQL
- `test_alembic_bridge.py` — bridge logic on DB
