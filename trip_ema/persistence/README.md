# `trip_ema/persistence/` — SQLAlchemy Core repositories

Every table access goes through a repository. Repos take a
`Connection` in their constructor — they don't own the engine. The
caller controls the transaction (typically `with transaction(engine) as conn:`).
Repos return domain objects, never tuples or ORM rows.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports `transaction`, the engine factory, and the repository classes |
| `connection.py` | Single source of truth for engine creation. Role-aware (Phase 12): app role for the service, read-only role for the GUI, migration role for Alembic |
| `tables.py` | SQLAlchemy Core `Table` definitions, mirroring the migration SQL one-to-one. Used to build queries — **not** to create the schema. Migrations apply SQL directly; keeping these in sync is manual discipline |
| `bars_repo.py` | OHLCV bars. Hot read path: `fetch_history(ticker_id, timeframe, limit)` (engine priming) and `fetch_since(ticker_id, timeframe, since)` (incremental ingest) |
| `tickers_repo.py` | `tickers` table. Returns `TickerRef` domain objects |
| `signals_repo.py` | `signal_events` (unified in 0016, plural). Append-only `insert_event` with `INSERT … ON CONFLICT DO NOTHING`. `find_pending_for_subscriber` is the distributor's centrepiece query — batches undelivered events with stale-event suppression (newer event for same (ticker, timeframe) supersedes) |
| `exchange_state_repo.py` | Phase 14.6b `exchange_state` (one row per exchange). COALESCE semantics — writing one field never clobbers others |
| `signal_backtest_runs_repo.py` | Phase 15a `signal_backtest_runs`. Per-(exchange, ticker_id, timeframe) resume tracking via `computed_through_date` |
| `fundamentals_repo.py` | Phase 1 fundamentals — three repos in one file because the runner uses them together: `IndustryOverrideRepository`, `InstrumentIndustryRepository`, `InstrumentSharesRepository`, plus `FundamentalsRunRepository`, `DailyMarketCapRepository`, `QuarterlyFinancialsRepository`, `IndicatorStateRepository`, `SignalEventRepository` |
| `subscribers_repo.py` | `subscribers` + `subscriptions` + `notification_log` writes (never reads — the "have I sent this before" join lives on `SignalsRepository`) |
| `symbol_history_repo.py` | `symbol_history`. NSE renames a symbol → the ISIN matches an existing ticker → write a history row, update the ticker row in place to the new canonical symbol |
| `corporate_actions_repo.py` | `corporate_actions`. Two write paths: reconciler-emitted `candidate_revision` rows (status `pending_review`) and yfinance `Ticker.actions` `split` / `dividend` rows |
| `alembic_bridge.py` | Phase-12 bridge from the legacy `schema_migrations` table to Alembic. For databases that ran the legacy runner through 010, stamps them at Alembic's squashed initial revision |
| `migrator.py` | The tiny legacy SQL runner. Discovers `NNN_*.sql`, applies each in a transaction, records the version. Preserved for history; no longer drives startup migrations after Phase 12 — Alembic is exclusive |

## Conventions

- **Connection-injected**, never engine-owning.
- **Domain objects out**, never tuples or rows.
- **Caller controls transactions** via `transaction(engine)`.
- **No business logic** — query shapes only. Composition lives in
  `etl/runner.py`, `fundamentals/runners.py`, and `scheduler/*`.

## Why Core, not ORM

We want repos to return immutable domain dataclasses — not tracked
ORM rows whose mutation has side effects. SQLAlchemy Core lets us
write the queries we want, hand-roll the column lists, and stay out
of identity-map territory.

## Migrations

Alembic is the only path that runs on startup (Phase 12). The legacy
`NNN_*.sql` set under `migrations/` is preserved for history; Alembic
revision 0001 squashes them. The `alembic_bridge` module marks
already-migrated DBs at the squashed revision so they don't replay
history.
