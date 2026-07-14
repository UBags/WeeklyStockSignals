# `trip_ema/transports/http/` — FastAPI app

The HTTP transport. App factory, dependency container, schemas, the
pulse pub/sub. Routes themselves live in `routes/`.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `app.py` | `create_app() -> FastAPI`. Returns a configured app with all routers mounted. **Does not own runtime state** (registry, cache, schedulers) — those are wired separately via `dependencies.set_state()`, so tests can build an app and inject fakes. Participates in FastAPI's ASGI-lifespan protocol via the optional `shutdown_callback` |
| `dependencies.py` | The FastAPI `Depends(...)` providers. Reads from a module-level container that the entry point populates at startup. Tests override via `app.dependency_overrides`. Includes `require_admin_token` for write-side gating |
| `schemas.py` | HTTP request/response Pydantic models. Wrap/unwrap the internal `api.responses` types into wire-format JSON. Wire format and internal types evolve independently — most fields pass through 1:1, enums-by-value become human-readable strings on the wire |
| `orchestrator_schemas.py` | Phase 14 payloads for orchestrator admin + pulse endpoints. Kept separate from `schemas.py` only to keep the Phase 14 diff reviewable; no functional reason to stay split |
| `pulse_state.py` | `PulseState` — in-memory pub/sub. Concrete implementation of the `PulsePublisher` Protocol declared in `scheduler.market_orchestrator`. Lives here because the only consumers are HTTP (the SSE endpoint in `routes/pulse.py` and the snapshot endpoint) |

## Subdirectories

| Path | Purpose |
|---|---|
| `routes/` | All routers — `app.py` mounts them in `create_app()`. One module per concern. See `routes/README.md` |

## Wiring pattern

```
entry point (trip-ema serve)
   ├─ build config, engine, registry, cache, scheduler, pool
   ├─ dependencies.set_state(...)        # populate container
   ├─ app = create_app(shutdown_callback=stop_all)
   └─ uvicorn.run(app)

route handler
   def handler(svc = Depends(get_some_svc)):  # type-hint visible in route signature
       svc.do_thing()

test
   app = create_app()
   app.dependency_overrides[get_some_svc] = lambda: FakeSvc()
   client = TestClient(app)
```

## Why split `app` and `dependencies`

- `app.py` is concerned with mounting routers and wiring ASGI lifespan.
- `dependencies.py` owns the runtime-state container.
- Tests instantiate the app without the runtime state, then inject
  whatever they want. Production wires the state once at startup.

## Why `pulse_state` lives here, not in `scheduler/`

The `PulsePublisher` Protocol is declared in
`scheduler.market_orchestrator` (because the orchestrator publishes
into it). The **concrete implementation** lives here because the only
subscribers are HTTP endpoints. Moving it elsewhere would force a
back-import from scheduler → http, which we don't want.

## Wire-format vs internal types

`api.responses.TerseResponse` is an internal type. `schemas.TerseResponseOut`
is its wire form. The mapping is thin but explicit. We deliberately
don't `model_validate` the internal type into the wire type — they're
written to evolve independently.
