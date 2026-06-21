# `trip_ema/migrations/` — Alembic + legacy SQL

Alembic is the **only** migration path that runs on startup
(Phase 12). The legacy SQL set is preserved for history and is
squashed into Alembic revision `0001`.

## Layout

```
alembic_pkg/
    versions/
        0001_initial_schema.py          # squash of legacy 001-010
        0002_db_roles.py
        0003_drop_tickers_source_timeframe.py
        0004_exchange_state.py
        0005_signal_backtest_runs.py
        0006_fundamentals_phase1.py
        0007_etf_classification.py
        0008_instrument_shares.py
        0009_quarterly_financials.py
        0010_daily_market_cap.py
        0011_industry_index.py
        0012_industry_index_constituent.py
        0013_industry_index_value.py
        0014_indicator_state.py
        0015_signal_event.py
        0016_unify_signal_events.py
        0017_benchmark_stats.py
    sql/
        0001_initial_schema.sql         # raw DDL applied verbatim by 0001
        …                               # per-revision raw SQL referenced from versions/*.py

legacy/
    001_initial_schema.sql              # historical only — not applied
    002_signal_state.sql
    003_subscribers.sql
    004_partitions_seed.sql
    005_drop_ticker_signal_state.sql
    006_bars_updated_at.sql
    007_ticker_tz_source_timeframe.sql
    008_drop_notification_log_dedup_index.sql
    010_etl_schema.sql
    011_exchange_state.sql
    012_signal_backtest_runs.sql
    013_fundamentals_phase1.sql
    014_etf_classification.sql
    015_instrument_shares.sql
    016_quarterly_financials.sql
    017_daily_market_cap.sql
    018_industry_index.sql
    019_industry_index_constituent.sql
    020_industry_index_value.sql
    021_indicator_state.sql
    022_signal_event.sql
```

## How Alembic revisions are authored

Each `versions/NNNN_*.py` revision references a sibling
`alembic_pkg/sql/NNNN_*.sql` file and applies it verbatim:

- The raw SQL stays **human-readable**.
- The Pydantic / SQLAlchemy `op.*` API would obscure the partial
  indexes, COALESCE-aware partial-unique constraints, partitioning,
  and `date_trunc('week', ts AT TIME ZONE 'Asia/Kolkata')` idioms
  this codebase leans on.
- The `versions/*.py` body is a thin loader; `upgrade()` reads the
  sibling `.sql` and executes; `downgrade()` either reverses or raises.

## Bridge for legacy databases

`persistence/alembic_bridge.py` handles databases that already ran
the legacy SQL through version 010. It stamps them at Alembic's
`0001` (squashed) revision without re-running the DDL.

## Revision highlights

| Rev | What it does |
|---|---|
| `0001` | Squash of legacy 001–010. Tickers, bars, signals, subscribers, notification log, ETL tables |
| `0002` | Phase-12 role-aware connections — app / readonly / migration roles |
| `0003` | Drop `tickers.source` and `tickers.timeframe` (single-source-of-truth cleanup) |
| `0004` | `exchange_state` (Phase 14.6b) |
| `0005` | `signal_backtest_runs` resume tracking (Phase 15a) |
| `0006` | Phase-1 fundamentals — `instrument_industry`, `fundamentals_run`, override table |
| `0007` | ETF classification on `tickers` (`instrument`, `instrument_type`) |
| `0008` | `instrument_shares` (historical shares outstanding) |
| `0009` | `quarterly_financials` |
| `0010` | `daily_market_cap` — column is `market_cap NUMERIC(22,2)` (not `market_cap_inr`) |
| `0011` | `industry_index` |
| `0012` | `industry_index_constituent` |
| `0013` | `industry_index_value` |
| `0014` | `indicator_state` — industry rows keyed by `industry_name + trade_date` (no `ticker_id`, no `timeframe`) |
| `0015` | `signal_event` (singular) — for per-industry / per-ticker backfilled signals |
| `0016` | **Unify `signal_events` (plural) + `signal_event` (singular)** into one canonical table. Drops the singular. Adds `industry_name` FK, `regime_before` / `regime_after`, nullable `ticker_id` / `timeframe`, XOR CHECK constraint, partial unique indexes |
| `0017` | Phase 6-3 benchmark stats — `stock_benchmark_stats` + `industry_benchmark_stats`, recomputed weekly. Drives the email re-rank score (0.5·RS₂₆w + 0.3·industry_RS + 0.2·R²) |

## Schema invariants worth remembering

- `ohlcv_bars` stores only `1D` bars. Weekly aggregation is on-demand
  in the indicator engine — **weekly bars are never stored**.
- `signal_events` (plural) is the only signal table after `0016`.
- `daily_market_cap.market_cap` is `NUMERIC(22, 2)` — not
  `market_cap_inr`.
- `tickers` carries both `instrument` and `instrument_type` after
  `0007`.
- `instrument_industry` uses column `industry` (not `industry_name`).
- `indicator_state` industry rows have **no** `ticker_id` and **no**
  `timeframe` — keyed by `industry_name + trade_date`.

## Operator commands

```
# Apply pending migrations
alembic upgrade head

# Inspect current revision
alembic current

# Stamp a fresh DB without applying (rare; for restores)
alembic stamp head
```

The service runs `alembic upgrade head` at startup if
`config.database.run_migrations_on_startup` is true (default).
