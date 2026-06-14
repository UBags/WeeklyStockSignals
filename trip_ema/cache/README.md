# `trip_ema/cache/` — In-memory signal cache

Holds the latest `BarState` per ticker so the HTTP read path can answer
`GET /v1/signals/{exchange}/{symbol}` in microseconds without touching
Postgres.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `signal_cache.py` | `SignalCache` class — thread-safe dict of latest-snapshot-per-ticker |

## Single-writer invariant (3.2)

Exactly **one** writer is allowed: `ComputeScheduler` during normal
operation, and the priming code (`trip_ema.api.lifecycle`) during
startup or reprime. Many readers (the HTTP route handlers, the
`SubscriberDistributor`'s decision logic) are fine because reads are
lock-free dict lookups.

This invariant is why `uvicorn --workers N` is forbidden — N copies of
the cache would silently disagree.

## Shape

A single `dict[TickerKey, CachedSignal]` where:
- `TickerKey = (exchange, symbol, timeframe)`
- `CachedSignal` holds the latest `BarState`, the `signal_type`
  (BULLISH/NEUTRAL/BEARISH), and the `computed_at` timestamp

## Memory footprint

Configured by `config.yaml`:
- `cache.max_history_bars` — bars loaded into each `TickerEngine` at
  priming (default 1000)
- `cache.state_history_retain` — `BarState`s the engine keeps per
  ticker (default 200)

For the NSE universe (~2700 tickers), the cache is single-digit MB.

## Tests

- `tests/cache/test_signal_cache.py` — read/write semantics,
  thread-safety, key handling
