# `scripts/` — Operator and developer scripts

Shell scripts and short Python utilities for running, monitoring, and
debugging the deployed `trip-ema` service. Plus the two long-form
operator recipe docs.

## Lifecycle scripts

| Script | Purpose |
|---|---|
| `restart_service.sh` | Kill + restart `trip-ema`; poll `/healthz` until primed; print `cache_size`. Accepts a custom config path as `$1` |
| `gui_start.sh` | Idempotent GUI launcher — skip if already running; needs `$DISPLAY` |
| `setup_psql_access.sh` | One-shot setup for `~/.pgpass` so operators don't need to enter the DB password |
| `restart_service.sh` (`/path/to/other_config.yaml`) | Same script, alternate config |

## Health / smoke

| Script | Purpose |
|---|---|
| `health_check.sh` | Read-only one-shot operator dashboard — health, ticker counts, bar counts, exchange-state, alert counts, recent runs |
| `smoke_test_fast.sh` | ~5s smoke test — hits `/healthz` + one ticker history; non-destructive |
| `smoke_test_full.sh` | ~30s broader smoke test |

## Diagnostic

| Script | Purpose |
|---|---|
| `db_diagnostics.sh` | Common DB-level investigation queries (parked tickers, slow queries, table sizes) |
| `audit_dead_island.sh` | Run the import-graph invariant test (`tests/test_no_dead_island.py`) — catches circular or layer-violating imports |

## Probes (one-off investigation)

| Script | Purpose |
|---|---|
| `probe_etf_yfinance.py` | Manual probe of yfinance behavior for ETF tickers (debugging Phase 1 ETF subbranch) |
| `probe_yfinance_hist_mcap.py` | Manual probe of yfinance's `histMarketCap` field (Phase 4A market-cap source) |
| `spike_nse_xbrl.py` | Exploratory probe of NSE's XBRL feed — deferred per `docs/FUNDAMENTALS_PHASES_1_TO_8.md` §14 |

## Documentation

| File | Purpose |
|---|---|
| `operator_commands.md` | The canonical day-to-day operator reference. Service lifecycle, initial exchange bring-up, daily updates, fundamentals pipelines, inventory queries, alert counts, ETL status, the admin token, GUI-deferred endpoints. Copy-paste-ready |
| `debug_recipes.md` | Less-common diagnostic workflows — orchestrator ticks, pulse streams, log digging, Alembic operations, when-the-service-won't-start |

## When to reach for which

- **Routine ops** — `operator_commands.md`
- **Something's broken** — `debug_recipes.md` first, then logs
- **Code change validation** — `restart_service.sh` then `smoke_test_*.sh`
- **Investigating a new data source** — write a `probe_*.py` or
  `spike_*.py` script here, don't add to the main package
