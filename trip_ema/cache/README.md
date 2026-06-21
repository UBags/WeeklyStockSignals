# `trip_ema/cache/` — in-process signal cache

The warm read path. One immutable `CachedSignal` per
`(ticker_id, timeframe)`, written by the `ComputeScheduler`, read by
HTTP routes and the `SubscriberDistributor`.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports `SignalCache`, `CachedSignal`, `build_cached_signal` |
| `signal_cache.py` | `SignalCache` — dict-backed, single write-lock. Stores immutable `CachedSignal` snapshots; replacements are atomic at the per-key level. `build_cached_signal(engine)` is the canonical constructor |

## Concurrency

- `ComputeScheduler` is the **only writer** in production.
- Readers (HTTP routes, distributor) acquire **no lock**. They read
  whatever snapshot is currently in the dict — that snapshot is
  immutable, so a torn read is impossible.
- The single `threading.Lock` serialises *writes against writes*, not
  writes against reads. This guards against a hypothetical second
  compute path; in practice today there is only one.

## What's in a CachedSignal

A `CachedSignal` carries the latest `BarState` (close, EMAs, ATR,
bands, regimes), the `ChangeSignal` that fired on the most recent
state transition (if any), the bar timestamp, and a `computed_at`
wall-clock stamp. That is enough for the HTTP terse response and the
SubscriberDistributor's batch composition.

## Lifecycle

```
startup → api.lifecycle.prime_all_tickers
            → for each (ticker, tf): build TickerEngine, prime N bars
            → SignalCache.put(build_cached_signal(engine))

every minute → ComputeScheduler tick
                 → for each due engine: step new bars
                 → SignalCache.put(build_cached_signal(engine))
```

The cache is **not** persisted. On process restart, `prime_all_tickers`
re-primes from `ohlcv_bars`.
