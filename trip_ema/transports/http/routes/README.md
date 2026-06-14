# `trip_ema/transports/http/routes/` — Per-thematic route files

One file per thematic slice. Each file exports a `router` (`APIRouter`)
that `app.py` mounts.

## Public routes (no auth)

| File | Endpoints | Purpose |
|---|---|---|
| `health.py` | `GET /healthz`, `GET /readyz` | Liveness + readiness. Readyz includes `cache_size`, `primed`, `stale_tickers_count` informationally |
| `signals.py` | `GET /v1/signals/{exchange}/{symbol}`, `.../history` | Cached read of latest signal; bar-by-bar history (replays a fresh engine over the requested window — used by the GUI chart) |
| `compute.py` | `POST /v1/compute/*` | Stateless on-demand compute over caller-supplied bars |
| `pulse.py` | `GET /v1/pulse/stream` | Server-Sent Events stream of pulse events (one per orchestrator tick) |

## Admin routes (bearer-token gated)

| File | Endpoints | Purpose |
|---|---|---|
| `admin.py` | `POST /v1/admin/prime-all`, `POST /v1/admin/compute-now`, `GET /v1/admin/stale-tickers` | Misc admin actions |
| `admin_health.py` | `GET /v1/admin/health-snapshot` | The 8-card system-health dashboard (consumed by the GUI's System Health tab) |
| `admin_etl.py` | 7 routes: `run-eod`, `run-intraday`, `backfill`, `refresh-listings`, `recompute-tiers`, `unpark-all`; `GET /status`, `GET /parked` | ETL CLI surface re-exposed as HTTP |
| `admin_exchange_state.py` | `GET /v1/admin/exchange-state[/{ex}]` | Per-exchange last-action timestamps (Phase 14.6) |
| `admin_exchanges.py` | `GET /v1/admin/exchanges` | The configured exchange list (used by the GUI picker) |
| `admin_subscribers.py` | Full CRUD over `/v1/admin/subscribers/*` + `PUT /{id}/filters`, `POST /{id}/test-notification` | Subscriber management |
| `admin_notifications.py` | `GET /recent`, `GET /dlq`, `POST /dlq/{id}/replay`, `DELETE /dlq/{id}` | Notification log + DLQ workflow (Phase 12) |
| `admin_orchestrator.py` | `GET /status`, `POST /{ex}/run-now`, `POST /{ex}/pause`, `POST /{ex}/resume` | Per-market orchestrator control |
| `admin_signals.py` | `POST /v1/admin/signals/generate`, `GET /v1/admin/signals/generated` | **Phase 15b** — kick off / read backtest signal generation. Includes the `pg_advisory_xact_lock(hash(exchange))` concurrent-run guard |
| `fundamentals.py` | Many routes under `/v1/fundamentals/*` | Read + compute endpoints for the Phases 1-8 fundamentals vertical slice — `industry-indices`, `signal-events`, `indicator-state`, `compute-*`, `runs`, `run-eod-pipeline`, `compute-market-cap`, `compute-index-values`, `compute-indicator-state`, `compute-signals` |

## Patterns

- **Read endpoints** — synchronous, return 200 with the payload
- **Action endpoints** — async-action pattern:
  1. Validate query params (400 on bad input)
  2. Validate exchange against config
  3. Check executor available (503 if None)
  4. Open `etl_runs` row (kind, exchange, correlation_id)
  5. Submit worker function to `etl_executor`
  6. Return 202 with `run_id`
- **Phase 15b variation** — `admin_signals.generate` adds a per-exchange
  `pg_advisory_xact_lock` inside the same transaction as the SELECT-
  then-INSERT pair to serialize concurrent requests for the same
  exchange (prevents two POSTs both passing the "is one already
  running?" guard)

## Tests

- `tests/transports/http/` — full integration tests (DB-required)
- `tests/transports_pure/` — pure-Python tests against mocked DB
  fixtures

The `admin_signals.py` route has the highest test count (28 in
`tests/transports_pure/test_admin_signals.py`) because the concurrent-
run guard, advisory lock, and run-row lifecycle have many branches.
