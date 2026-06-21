# `trip_ema/api/` — public compute surface

Three entry points the rest of the codebase calls into. This is the
spec's (a)/(b)/(c) split: one stateless function for ad-hoc compute,
one cached read for the warm path, and one lifecycle helper for
startup priming.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports `compute_full_terse`, `compute_full_verbose`, `prime_all_tickers`, `TerseResponse`, `VerboseResponse` |
| `stateless.py` | Function **(a)** — `compute_full(...)` recomputes a TickerEngine from raw bars and returns Terse/Verbose. The ad-hoc / testing entry point — caller provides bars, engine is never cached |
| `cached.py` | Function **(b)** — read-only fetch from the `SignalCache`. The hot path used by HTTP `GET /v1/signal/...`. ComputeScheduler is the *only writer*; this side never mutates |
| `lifecycle.py` | Function **(c)** — `prime_all_tickers(...)` is the startup orchestrator. Iterates the configured exchanges, builds a `TickerEngine` per ticker primed with N historical bars, and registers each into the `TickerRegistry` + `SignalCache`. Also exposes `_build_cached_signal_from_engine` for the fundamentals runners |
| `responses.py` | `TerseResponse` (one-bar latest state) and `VerboseResponse` (per-bar state history up to N bars). Frozen Pydantic v2 models — the public DTO surface |

## Invariants

- `stateless` is the **only** path that constructs a `TickerEngine`
  outside startup. The warm path uses cached engines via the registry.
- `cached` never mutates. It reads whatever entry is in the dict;
  reads take no lock.
- `responses.*` types are the only types HTTP routes ever serialize.
  Internal `BarState` / `ChangeSignal` types stay private.

## Read paths

```
HTTP route → api.cached → SignalCache → CachedSignal → TerseResponse
HTTP route → api.stateless → fresh TickerEngine → VerboseResponse
startup    → api.lifecycle.prime_all_tickers → engines into registry
```
