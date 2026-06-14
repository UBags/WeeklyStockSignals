# `alembic_pkg/` — Alembic migration package (canonical schema source)

Phase 12 made Alembic the **exclusive** migration system for `trip-ema`.
Every schema change ships as an Alembic revision here; the legacy SQL
files in `../migrations/legacy/` are archived for reference + parity
testing only.

## Contents

| Path | Purpose |
|---|---|
| `env.py` | Alembic environment — loads `config.yaml`, builds the SQLAlchemy URL, runs migrations online/offline |
| `script.py.mako` | Template Alembic uses when generating new revision files |
| `README` | The default Alembic-generated README (essentially empty) |
| `sql/` | Optional SQL bodies that Alembic revisions execute via `op.execute(text(open(...).read()))` |
| `versions/` | The Alembic revision files — one per schema change |

## Revision catalogue

| Revision | Adds / Changes | Notes |
|---|---|---|
| `0001_initial_schema` | Squashed schema — tables, indexes, partitions seeded by legacy files 001-010 | Pre-Phase-12 DBs are stamped at this revision via the alembic bridge without re-running DDL |
| `0002_db_roles` | `trip_etl_user`, `trip_engine_user`, `trip_gui_user` + per-table GRANTs | Roles created `NOLOGIN`; operators grant LOGIN + set passwords |
| `0003_drop_tickers_source_timeframe` | Removes obsolete columns from `tickers` | Cleanup post-Phase 11 |
| `0004_exchange_state` | `exchange_state` table + NSE seed | Phase 14.6 — per-exchange last-action timestamps |
| `0005_signal_backtest_runs` | `signal_backtest_runs` table + `etl_runs.exchange` column | Phase 15a — backtest resume tracking |
| `0006_fundamentals_phase1` | `instrument_industry` + `industry_override` + `fundamentals_run` tables | Fundamentals Phase 1 — industry classification |
| `0007_etf_classification` | Adds `INSTRUMENT_TYPE` to classification_source enum | Fundamentals Phase 1 ETF subbranch |
| `0008_instrument_shares` | `instrument_shares` table | Fundamentals Phase 2 — shares-outstanding time series |
| `0009_quarterly_financials` | `quarterly_financials` table | Fundamentals Phase 3.A — revenue/ebitda per quarter |
| `0010_daily_market_cap` | `daily_market_cap` table | Fundamentals Phase 4A |
| `0011_industry_index` | `industry_index` table | Fundamentals Phase 4B — index definitions |
| `0012_industry_index_constituent` | `industry_index_constituent` table | Fundamentals Phase 4B — composition |
| `0013_industry_index_value` | `industry_index_value` table | Fundamentals Phase 4B — daily index values |
| `0014_indicator_state` | `indicator_state` table | Fundamentals Phase 5 — per-(industry or ticker) Triple-EMA stack |
| `0015_signal_event` | `signal_event` table | Fundamentals Phase 6-1 — BUY/SELL/EXIT events |

## Authoring a new migration

```bash
pip install trip-ema[migrations]
alembic revision -m "describe what this migration does"
# edit alembic_pkg/versions/<rev>_<slug>.py — use the op API
alembic upgrade head
```

If the new revision creates / drops a table or alters columns in a way
that changes the table set, **also** add a byte-equivalent legacy SQL
file under `../migrations/legacy/NNN_*.sql` to keep the legacy-parity
test (`tests/persistence/test_legacy_migrator.py`) green. See
`../migrations/legacy/README.md` for the obligation.

## Runtime use

`trip-ema` runs `alembic upgrade head` at startup. Failure is **fatal**
(was a logged warning during Phase 11; tightened in Phase 12).

For pre-Phase-12 databases that ran the legacy migrator through 010,
`trip_ema.persistence.alembic_bridge.stamp_baseline_if_needed()` detects
a populated `schema_migrations` table on first startup and stamps
`0001_initial_schema` **without** re-running DDL. Subsequent
`alembic upgrade head` then applies any post-squash revisions.

## Configuration

Driven by `../alembic.ini`:
- `script_location = alembic_pkg`
- `sqlalchemy.url` is resolved at runtime by `env.py` from
  `config.yaml`'s `database:` block

`env.py` resolves the password env-var (`password_env: TRIP_EMA_DB_PWD`)
the same way the runtime app does.
