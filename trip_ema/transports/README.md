# `trip_ema/transports/` — HTTP transport layer

The integration boundary the operator console, external automation, and
cron jobs talk to. FastAPI app + routes; admin gating via bearer token;
async-action pattern for long-running operations.

## Subpackages

| Path | Purpose |
|---|---|
| `http/` | FastAPI app, dependencies, schemas, routes |

There's no other top-level transport today. Future SSE expansions or a
gRPC transport would land alongside `http/` here.

## Architecture summary

See `http/README.md` for detail. High-level:

- **One FastAPI app** built in `http/app.py`
- **Router decomposition** by theme — health, signals, compute, pulse,
  admin-* (etl, exchange-state, exchanges, subscribers, notifications,
  orchestrator, health, reprime, circuit-breakers, signals (Phase 15b))
- **Bearer-token gate** for every admin route via
  `http/dependencies.py::require_admin_token`. Env-var name comes from
  `http.admin_token_env`; defaults to `TRIP_EMA_ADMIN_TOKEN`
- **AppState singleton** — `db_engine`, `cache`, `registry`,
  `etl_executor`, `config`, `orchestrator_pool`, ... — injected into
  routes via `dependencies.py`
- **Async-action pattern** for heavyweight admin POSTs: open an
  `etl_runs` row in the request thread, submit work to `etl_executor`,
  return 202 with `run_id`

## Async-action pattern

```
@router.post("/v1/admin/some-action")
def admin_some_action(...):
    validate_query_params()
    validate_exchange()
    require_executor()  # raise 503 if None
    run_id = etl_repo.open_run(kind="some_action", exchange=exchange, ...)
    etl_executor.submit(_run_some_action_safe, run_id, ...)
    return 202, {"run_id": run_id, ...}
```

Phase 15b variation in `routes/admin_signals.py`: adds
`pg_advisory_xact_lock(hash(exchange))` inside the same transaction
as the SELECT-then-INSERT pair to serialize per-exchange concurrent
requests.

## Tests

- `tests/transports/` — full HTTP integration tests (DB-required) for
  the main routers
- `tests/transports_pure/` — pure-Python tests against mocked DB
  fixtures — admin_signals (28 tests), admin_etl, admin_subscribers, ...
