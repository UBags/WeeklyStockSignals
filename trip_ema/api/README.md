# `trip_ema/api/` — Service lifecycle (priming + cached/stateless boundaries)

The thin layer that sits between FastAPI route handlers and the engine/
cache/registry. Owns priming (loading historical bars into engines at
startup), and provides the two read paths the HTTP layer exposes:
**cached** (fast, in-memory) and **stateless** (recompute on demand).

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `lifecycle.py` | `prime_all_tickers(...)` — walk every active ticker, fetch history, build `TickerEngine` state, populate `SignalCache`. Runs at startup; can be re-invoked via `POST /v1/admin/prime-all` |
| `cached.py` | Read path against `SignalCache` — fast, single-writer reads |
| `stateless.py` | Stateless compute path — accepts caller-supplied bars, runs an ephemeral engine, returns the result without touching cache or DB |
| `responses.py` | Pydantic response models shared between the cached + stateless paths |

## How priming works

```
prime_all_tickers(registry, cache, bars_repo, config)
    for ticker in registry.all():
        bars = bars_repo.fetch_history(ticker, limit=config.cache.max_history_bars)
        engine = TickerEngine(ticker, ...)
        engine.prime(bars)                  # one-shot bulk; no events emitted
        registry.set_engine(ticker, engine)
        cache.write(ticker, engine.latest_state())
```

Priming is **idempotent** — repeated runs produce the same end state.
The service refuses to serve requests until priming completes (gates
the readiness check via the `cache_size > 0` predicate in `/readyz`).

## Why "cached" vs "stateless"

| Path | When | How |
|---|---|---|
| Cached (`/v1/signals/{exchange}/{symbol}`) | Operator / GUI reading "what's the latest signal?" | `O(1)` cache lookup — no compute |
| Stateless (`/v1/compute/*`) | External caller wants to compute against bars they hold (e.g. simulation, what-if) | Build ephemeral engine, ingest caller's bars, return result, discard engine |

Both paths share the response shape via `responses.py`.

## Single-writer invariant

`SignalCache` has exactly one writer: `ComputeScheduler` during normal
operation, and `prime_all_tickers` during startup / reprime. The cached
read path here is **read-only** against the cache.

## Tests

- `tests/api/test_cached.py` — cached read path
- `tests/api/test_stateless.py` — stateless compute path
- `tests/api/test_lifecycle.py` — priming behavior, idempotency
- `tests/api/test_end_to_end.py` — full priming → compute → read sequence
