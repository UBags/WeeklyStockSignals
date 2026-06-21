# `trip_ema/indicators/` — Triple-EMA + ATR engine

A faithful Python port of the operator's TradingView Pine indicator.
Pure-function primitives (atr, vw_ema, bands, regime classifiers)
composed by a stateful per-(ticker, timeframe) engine. The engine is
the source of truth for live signals.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports `TickerEngine` and the primitive functions |
| `ticker_engine.py` | `TickerEngine` — the stateful composer. Lifecycle: `__init__(ticker, timeframe, params)` → `prime(historical_bars)` (one-shot fold) → `ingest(latest_bar)` (O(1) per bar). Holds the previous regime, the per-EMA running state, and the latest `BarState`. Not thread-safe — caller serialises |
| `atr.py` | Wilder's ATR (`ta.atr` port). α = 1/length, seed = SMA of first `length` true-range values. **Not** the standard EMA α = 2/(length+1) |
| `vw_ema.py` | Volume-weighted triple-stage EMA. Pine construction: `vw1 = ema(c*v, base) / ema(v, base)`; `vw2 = ema(vw1, s1)`; polished WMA filter; optional final EMA |
| `bands.py` | Pure-stateless ATR bands + pierce detection. Constructed relative to the *highest* / *lowest* of the three EMAs (hugs the ribbon's outer extents, not its centre) |
| `aggregation.py` | Bar aggregation across timeframes. Native fetch is 15m / hourly / 2-hourly / daily; weekly is aggregated internally with each exchange's local-Monday anchor |
| `core_regime.py` | Port of Pine's `core_background` state machine. Pure stepping function: `(prev_core, current_inputs) -> new_core`. State lives in the engine, not here |
| `display_regime.py` | Port of Pine's `display_background` gate layer. Takes a core regime and decides whether to actually "paint" it. Stricter on fresh entries (must be in the Goldilocks corridor — past inner band but not past outer), looser on continuations and flips from the opposite colour |
| `change_classifier.py` | Display-regime transition → `ChangeSignal`. Used by the engine for `latest_change` and by the `ComputeScheduler` for the cross-tick change events that drive notifications |

## Pipeline

```
RawBar
   │
   ├─ aggregation (if non-native timeframe)
   │
   ├─ vw_ema   ───► three EMAs
   ├─ atr      ───► ATR
   │
   ├─ bands         (EMAs + ATR → inner/outer bands)
   ├─ core_regime   (prev_core + bar → new_core)
   ├─ display_regime (core + bands  → display)
   │
   └─ change_classifier (prev_display → new_display → ChangeSignal)
                                            │
                                            ▼
                                       BarState
```

## Invariants

- Primitives (`atr`, `vw_ema`, `bands`, `core_regime`, `display_regime`,
  `change_classifier`) are **pure**. No mutable state.
- The engine is the **only** module that holds state across bars.
- `prime` is idempotent on the same inputs.
- `ingest` is O(1) — no recomputation over history.
- Same code path runs live and in `fundamentals/signal_generator.py`
  for historical replay, so behaviour matches by construction.

## Pine-port fidelity

We reproduce Pine's exact semantics — Wilder's α for ATR, WMA filter
in `vw_ema`, band construction off the outer EMAs. Any deliberate
divergence from Pine must be documented at the call site.
