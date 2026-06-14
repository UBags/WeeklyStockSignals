# `trip_ema/` — Main package

The Python package distributed as `trip-ema` on PyPI. Implements the
Triple-EMA + ATR weekly indicator (Pine Script port) plus the full
production service around it.

## Entry points

Defined in `pyproject.toml`:

| Command | Function | Process |
|---|---|---|
| `trip-ema` | `trip_ema.app:main` | Main service — FastAPI + scheduler |
| `trip-ema-notifier` | `trip_ema.notifier_worker:main` | Notifier worker — pops Redis jobs, dispatches transports |
| `trip-ema-etl` | `trip_ema.etl.cli:main` | ETL CLI — yfinance ingest + Phase 15 backtest |
| `trip-ema-console` | `trip_ema.gui.console:main` | PySide6 operator GUI |

## Top-level files

| File | Purpose |
|---|---|
| `__init__.py` | Package marker + `__version__` (via `importlib.metadata`, fallback hardcode) |
| `app.py` | Main service entry — config load, DB engine, Alembic upgrade, registry/cache build, prime_all, orchestrator pool, transports wiring, FastAPI launch |
| `notifier_worker.py` | Notifier-worker entry — pops jobs from Redis, checks staleness + breaker, dispatches, writes `notification_log`/`notification_dlq` |

## Subpackages (layered)

The codebase is layered. Lower layers may not import from higher layers.

| Layer | Path | Role |
|---|---|---|
| 1 — Domain | `domain/` | Pure types: `BarState`, `OHLCV`, enums |
| 2 — Pure math | `indicators/` | VW-EMA, ATR, bands, weekly aggregation |
| 3 — Engine | `engine/` | `TickerEngine`, core/display regime, change classifier |
| 4 — Persistence + cache | `persistence/`, `cache/` | Repos, in-memory `SignalCache` |
| 5 — Compositional | `etl/`, `registry/`, `config/`, `fundamentals/` | Higher-order glue, configuration, ETL fetchers, fundamentals vertical slice |
| 6 — Schedulers + notifications | `scheduler/`, `notifications/` | `ComputeScheduler`, `MarketOrchestrator`, `SubscriberDistributor`, transports, breakers |
| 7 — Boundary | `transports/`, `api/`, `gui/`, `notifier_worker.py`, `app.py` | HTTP API, lifecycle (priming), GUI, separate-process worker |
| Cross | `observability/` | Logging, metrics, tracing, correlation IDs (consumed by every layer) |

## Invariant 3.14 — process boundaries

`trip-ema-etl`, `trip-ema` (main service / engine), `trip-ema-console`
(GUI), and `trip-ema-notifier` are **separate Python processes**, not
modules running in the same process. Enforced by:
- Distinct entry-point functions and CLI commands
- Per-process least-privilege DB roles (Phase 12, opt-in)
- The dead-island import test (`tests/test_no_dead_island.py`)

## Other repo-root pointers

- `pyproject.toml` — packaging, extras, deps, entry points
- `config.yaml` — runtime config (referenced via `--config`)
- `alembic.ini` + `alembic_pkg/` — DB migrations
- `migrations/legacy/` — archived legacy SQL + parity parallels
- `docs/`, `01_FUNCTIONALITY.md`, `02_ARCHITECTURE.md`, `03_OPEN_ITEMS.md` — documentation
