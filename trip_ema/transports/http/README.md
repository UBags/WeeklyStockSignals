# `trip_ema/transports/http/` — FastAPI HTTP layer

The `trip-ema` service's HTTP surface. FastAPI app, dependency injection,
Pydantic schemas, and the per-thematic-slice route routers.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `app.py` | `create_app(state, config)` — builds the FastAPI instance, registers routers, installs middleware (correlation-ID, observability) |
| `dependencies.py` | DI factories — `get_db_engine`, `get_config`, `get_etl_executor`, `get_cache`, `get_registry`, `require_admin_token` (bearer-token gate) |
| `schemas.py` | Shared Pydantic request / response models — `SignalGenerateRequest`, `SignalGenerateResponse`, ... (one section per phase that added schemas) |
| `orchestrator_schemas.py` | Schemas specific to the orchestrator routes (status, pause/resume, run-now) |
| `pulse_state.py` | Server-Sent Events plumbing for `/v1/pulse/stream` — broadcaster pattern with async subscribers |

## Subdirectories

| Path | Purpose |
|---|---|
| `routes/` | Router files, one per thematic slice (health, signals, compute, pulse, admin-*) |

## App composition

```
create_app(state, config) -> FastAPI
    install correlation-id middleware
    register public routers      (health, signals, compute, pulse)
    register admin routers       (admin, admin_etl, admin_exchange_state,
                                  admin_exchanges, admin_signals (P15b),
                                  admin_subscribers, admin_notifications,
                                  admin_orchestrator, admin_health, admin_reprime,
                                  admin_circuit_breakers)
    install OpenAPI tags / descriptions
    return app
```

The app is then handed to `uvicorn.run(app, host=..., port=...)` from
`trip_ema.app:main`.

## Dependency injection pattern

Routes declare their dependencies via FastAPI's `Depends()`:

```python
@router.post("/v1/admin/etl/run-eod")
def admin_etl_run_eod(
    exchange: str,
    db: Engine = Depends(get_db_engine),
    executor: ThreadPoolExecutor = Depends(get_etl_executor),
    config: Config = Depends(get_config),
    _: None = Depends(require_admin_token),
):
    ...
```

`AppState` is the singleton that holds the actual instances; the
`get_*` factories pull from it. Route handlers don't know about
`AppState` directly.

## Admin gating

`require_admin_token` reads `Authorization: Bearer <token>`, compares
to `os.environ[config.http.admin_token_env]`:
- Env var not set → 503 Service Unavailable
- Missing / malformed header → 401 Unauthorized
- Token mismatch → 403 Forbidden

The env-var name (not the value) is in config so operators can rotate
tokens without redeploying.

## Tests

- `tests/transports/http/` — full integration tests (DB-required) per
  route: `test_health.py`, `test_compute.py`, `test_signals.py`,
  `test_admin.py`, `test_admin_orchestrator.py`, `test_pulse_routes.py`,
  `test_fundamentals_routes.py`, `test_create_app_step6.py`
- `tests/transports_pure/` — pure-Python tests against mocked DB:
  `test_admin_signals.py` (28 tests, Phase 15b), `test_admin_etl.py`,
  `test_admin_exchanges.py`, `test_admin_exchange_state.py`,
  `test_admin_circuit_breakers.py`, `test_admin_reprime.py`,
  `test_admin_slice4.py`, `test_signal_history.py`,
  `test_graceful_shutdown.py`
- `tests/transports/test_pulse_state.py` — SSE plumbing
