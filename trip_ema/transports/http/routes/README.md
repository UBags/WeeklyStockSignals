# `trip_ema/transports/http/routes/` — FastAPI routers

One router per concern. Mounted by `transports/http/app.py`. Admin
routers gate writes with `require_admin_token`; pulse + health are
unauthenticated.

> Import convention: files here use **4-dot** relative imports to
> reach the top-level package: `from ....api import ...`,
> `from ....persistence import ...`, etc. 3-dot resolves to a
> nonexistent `trip_ema.transports.X` — guaranteed to be the cause of
> any `ModuleNotFoundError` after a refactor.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `health.py` | `GET /healthz` (always 200 if the process is up — no DB or dependency check). `GET /readyz` (200 only if registry + cache non-empty, `ComputeScheduler` alive and ran within 2× its cadence, `SubscriberDistributor` alive). Used by load balancers |
| `pulse.py` | **Unauthenticated** read-only orchestrator pulse. `GET /v1/pulse` (snapshot of recent events), `GET /v1/pulse/stream` (SSE of new events). Carries no secrets — only the same orchestrator statuses the operator can see via admin endpoints. Reverse-proxy basic auth is the answer if the posture changes |
| `compute.py` | `POST /v1/compute` — the **(a)** stateless path. Builds a throwaway `TickerEngine`, primes with supplied history, ingests the latest bar, returns Terse or Verbose. **Does not touch cache, DB, or registry**. Use for ad-hoc analysis or tickers not in the warm set |
| `admin.py` | Core admin levers (bearer-token gated). `POST /v1/admin/prime-all`, `POST /v1/admin/compute-now`, `GET /v1/admin/stale-tickers`, `POST /v1/admin/reprime-ticker/{exchange}/{symbol}`, `GET /v1/admin/circuit-breakers` (legacy mode only — queue mode says so) |
| `admin_etl.py` | ETL action surface for the GUI. Each "fire" endpoint submits the runner to `AppState.etl_executor`, returns **202 Accepted** with the `run_id` (or `None` if the runner skipped, e.g. intraday on a holiday). GUI polls `GET /v1/admin/etl/status` |
| `admin_exchange_state.py` | Phase 14.6b. `GET /v1/admin/exchange-state` (all rows), `GET /v1/admin/exchange-state/{exchange}` (one row, 404 if absent). Runner writes inside the same transaction that closes `etl_runs`, so `last_eod_run_at` here implies the `etl_runs` row reflects that same run |
| `admin_exchanges.py` | Phase 14.6c. `GET /v1/admin/exchanges` — what's **available** for ETL actions (sourced from `config.market_hours`). Sibling of `/exchange-state` (what's *active*) |
| `admin_health.py` | Slice 4. `GET /v1/admin/health-snapshot` — list of "cards" (status / headline / detail) per logical subsystem. The GUI's System Health dashboard renders these as a colour-coded grid. Cheap to compute — one SQL pass for DB-derived counts |
| `admin_notifications.py` | Slice 4 + Phase 12 DLQ. `GET /v1/admin/notifications/recent` (joined log entries), `GET /v1/admin/notifications/dlq`, DLQ requeue / drop endpoints |
| `admin_orchestrator.py` | Phase 14. `GET /v1/admin/orchestrator/status` (per-market), `POST /v1/admin/orchestrator/{exchange}/run-now`, `POST /v1/admin/orchestrator/run-now-all`. Fire-and-forget |
| `admin_signals.py` | Phase 15b. `POST /v1/admin/signals/generate` (fires a backtest signal-generation run, returns 202 with `etl_runs.run_id`; concurrent-run guard per exchange). Plus the signal-history reader the GUI uses |
| `admin_subscribers.py` | Slice 4 CRUD over `subscribers` + child `subscriptions`. `GET / list`, `GET /{id}`, `POST`, `PATCH`, `DELETE`, plus `POST /{id}/send-test` (test-notification email). Primary consumer: the GUI's Subscribers tab |
| `fundamentals.py` | Phase 1 reads and admin actions. Reads under `/v1/fundamentals/*` (industry classifications, coverage stats, recent runs). Admin writes under `/v1/admin/fundamentals/*` (override CRUD, refresh fires, dry-run + commit modes) |

## Conventions

- **Bearer-token writes.** Anything that mutates state goes through
  `require_admin_token`. The GUI degrades to read-only when the token
  is unset.
- **Fire-and-forget for long jobs.** Returns 202 + `run_id`; the
  client polls a status endpoint. Never block a request on a backfill.
- **DTOs come from `schemas.py`.** Routes never import internal
  domain types directly.
- **Concurrent-run guards.** Anything that operates on
  `(exchange, ticker, timeframe)` and could be invoked twice
  (`/generate`, `/run-now`, …) holds a per-key lock for the duration.

## Long-running endpoints

| Endpoint | Status path |
|---|---|
| `/v1/admin/etl/fire/*` | `/v1/admin/etl/status` |
| `/v1/admin/signals/generate` | `/v1/admin/signals/runs` |
| `/v1/admin/orchestrator/*/run-now` | `/v1/admin/orchestrator/status` |
| `/v1/admin/fundamentals/*/refresh` | `/v1/admin/fundamentals/runs` |

All return **202** + an identifier. None block.

## Pulse vs admin

The pulse stream is read-only and unauthenticated. Anything that
*does* something — fires a job, mutates a row, changes config — is
admin-gated.
