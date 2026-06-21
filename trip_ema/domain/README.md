# `trip_ema/domain/` — frozen domain types

The lowest layer. Pure dataclasses + enums. No I/O, no clock,
no imports from any other `trip_ema` module. Every other package
imports **from** `domain`; `domain` imports **from** nothing.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports the public type surface: `TickerRef`, `Timeframe`, `RawBar`, `BarState`, `CoreRegime`, `DisplayRegime`, `ChangeSignal`, `IndicatorParams`, `SignalEvent`, … |
| `ohlcv.py` | `RawBar` (open/high/low/close/volume + ts), `TickerRef` (ticker_id, symbol, exchange, instrument, instrument_type). The lowest-level structural types — no logic |
| `state.py` | Indicator state: `Timeframe` enum, `IndicatorParams` (EMA lengths, ATR length/multipliers, lookbacks), `BarState` (per-bar EMA/ATR/band values + regime), `CoreRegime` / `DisplayRegime` / `ChangeSignal` enums. All immutable |
| `events.py` | Persistence / notification surface: `SignalEvent`, `NotificationBatch`, `NotificationBatchItem`. Frozen, datetime-aware |

## Invariants

- Everything is frozen / immutable. Mutation lives in the engine
  (`indicators/ticker_engine.py`), never in domain types.
- No I/O imports. No `yaml`, no `requests`, no `sqlalchemy`.
- No imports from any other `trip_ema` sub-package — this is the
  acyclicity root.

## Why a separate package

Two reasons:
1. **Dependency hygiene.** Anyone (config, persistence, indicators,
   etl, gui, …) can import these types without dragging in I/O.
2. **Pickling / multi-process boundary.** When workers ship batches
   across the redis queue or backtests across worker processes, only
   `domain` types travel. They're already designed to be safe to
   pickle.
