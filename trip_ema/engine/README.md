# `trip_ema/engine/` — Per-ticker engine + regime classification

The stateful core of the system. Each ticker has one `TickerEngine`
instance that ingests bars, runs the indicator stack, and emits
`BarState`s. Regime classification then turns the bar's state into the
three-state regime (`BULL` / `NEUTRAL` / `BEAR`) and into change signals
(`BUY` / `SELL` / `EXIT_*` / `NO_CHANGE`).

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `ticker_engine.py` | `TickerEngine` — the per-ticker stateful object. Two entry points: `prime(historical_bars)` for one-shot bulk and `ingest(bar)` / `ingest_provisional(bar)` for live operation |
| `core_regime.py` | Computes BULL/NEUTRAL/BEAR from the EMA stack + ATR-band positioning. The "underlying" regime |
| `display_regime.py` | Gates `core_regime` — filters spurious flips (e.g. one-bar dips). The state actually shown to subscribers |
| `change_classifier.py` | Turns a `display_regime` transition into a `ChangeSignal` (`BUY` / `SELL` / `EXIT_FROM_BUY` / `EXIT_FROM_SELL` / `NO_CHANGE`) |

## Pine-faithful semantics

- **NaN warmup propagation.** Until each EMA has enough bars to settle,
  it returns NaN — exactly as Pine Script does. `BarState`s during
  warmup have NaN slots; downstream consumers must handle that
- **Wilder seeding for ATR.** ATR uses Wilder's RMA seeding (first
  bar's TR as the seed, then exponential smoothing) — matches Pine
- **Triple-stage EMA.** Each of med/long/longest goes through three
  smoothing stages (`base → s1 → s2 → final`), with parameters from
  `config.ema`. The `med` band has no `final` stage in Pine
- **Parity asserted bar-by-bar** via `tests/parity/test_pine_parity.py`
  with `1e-9` absolute tolerance

## Provisional / final semantics

`TickerEngine.ingest_provisional(bar)` accepts an intra-bar update for a
not-yet-closed weekly bucket. The engine snapshots its state first and
can roll back when the next *final* bar arrives — so live intraday ticks
don't permanently mutate state until the bar closes.

This is invariant 3.6 — re-applying a provisional bar must produce the
same end state.

## How a bar flows

```
OHLCV bar
  └─ TickerEngine.ingest(bar)
       ├─ 3-stage VW-EMA  (med/long/longest)
       ├─ ATR + bands
       └─ produce BarState  ┐
                            ├─ CoreRegime  (BULL/NEUTRAL/BEAR)
                            ├─ DisplayRegime  (gated)
                            └─ classify_change(prev_display, new_display)
                                  → ChangeSignal
```

## Used by

| Caller | How |
|---|---|
| `ComputeScheduler` (live path) | `engine.ingest(bar)` after fetching new bars |
| `prime_all_tickers` (startup) | `engine.prime(history)` to build initial state |
| `signal_generator` (Phase 15 backtest) | Builds **fresh** engines per ticker × timeframe; replays all history |
| `transports.http.routes.signals.get_signal_history` | Builds a fresh engine to replay a window for the chart view |

## Tests

- `tests/engine/test_ticker_engine.py` — ingest semantics, priming, provisional rollback
- `tests/engine/test_core_regime.py` — core regime computation
- `tests/engine/test_display_regime.py` — gating logic
- `tests/engine/test_change_classifier.py` — transition classification
- `tests/parity/test_pine_parity.py` — bar-for-bar Pine numerical parity
