# `trip_ema/registry/` — Ticker registry (engine map)

The in-memory map from `(exchange, symbol, timeframe)` to its
`TickerEngine` instance. Built at startup; consulted by the scheduler
on every tick.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `ticker_registry.py` | `TickerRegistry` — thread-safe dict + builder methods (`set_engine`, `get_engine`, `iter_engines`, `prime_from_db`) |

## Lifecycle

```
TickerRegistry()                            # empty
   prime_from_db(engine, ticker_repo, bars_repo, cache)
      for ticker in ticker_repo.iter_active():
         bars = bars_repo.fetch_history(ticker, max_history_bars)
         engine = TickerEngine(ticker, params)
         engine.prime(bars)
         self.set_engine(ticker, engine)
         cache.write(ticker, engine.latest_state())
```

This is invoked from `trip_ema.api.lifecycle.prime_all_tickers`.

## Reprime semantics

The `POST /v1/admin/reprime?exchange=X&symbol=Y` admin endpoint
discards the engine for one ticker and rebuilds it from DB history.
The atomic ownership: only the registry mutates the engine map; the
scheduler reads (and calls `engine.ingest(...)` which mutates the
engine itself, not the map).

## Why a registry instead of "engine pool"

- Each ticker has its own state (EMA accumulators, ATR seed, regime
  history). Engines are not interchangeable
- Building an engine is moderate cost (~10ms per ticker; ~30s for the
  full NSE universe); doing it lazily would add latency to the first
  request per ticker after a startup
- Priming all engines at startup means `/readyz` truthfully says
  "every ticker is computable" once the registry returns

## Tests

- `tests/registry/test_ticker_registry.py` — set/get, iteration,
  reprime, thread-safety
