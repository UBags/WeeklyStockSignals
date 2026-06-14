# `trip_ema/indicators/` — Pure math (Layer 2)

Volume-weighted EMAs, ATR, ATR bands, and weekly aggregation — the
numerical core. Pure Python (with NumPy for performance); no DB, no
config, no threading.

Pine Script numerical fidelity is **non-negotiable** (invariant 3.3).
Every function here is faithful to Pine's recurrence and NaN-warmup
semantics. Verified bar-for-bar by `tests/parity/test_pine_parity.py`
with `1e-9` absolute tolerance.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `vw_ema.py` | Triple-stage volume-weighted EMA (`base → s1 → s2 → final`). Pine-faithful NaN propagation during warmup. Used for `med`, `long`, `longest` |
| `atr.py` | ATR using Wilder's RMA seeding — first bar's TR is the seed, then exponential smoothing |
| `bands.py` | Six ATR bands: inner/mid/outer × upper/lower, each at a configured multiplier of ATR around the close |
| `aggregation.py` | `daily_to_weekly(bars, tz)` — Pine-anchored weekly buckets from daily bars. `_monday_of`, `bucket_end` helpers. Also detects gaps |

## NaN semantics

Pine emits NaN until each EMA's stage has accumulated enough bars to
produce a meaningful value. The Python port mirrors this — early bars
return NaN, downstream consumers must handle that. `BarState`s during
warmup have NaN slots; the `signal_event` writer suppresses transitions
involving NaN.

## Weekly aggregation

`daily_to_weekly(bars, tz)` is Pine-anchored:
- Bar's bucket starts on Monday 00:00 in the specified timezone
- The bucket's OHLCV is the standard aggregation (open from the first
  daily bar, high/low from the max/min, close from the last, volume
  from the sum)
- DST transitions handled via `tz`-aware datetimes; the bucket boundary
  is wall-clock Monday in the exchange's local time

Gap detection: `weekly_buckets_with_bars` returns the inferred bucket
plus a flag for each bucket that should exist but has no daily bars.
Used by `ComputeScheduler` to detect ETL backlogs.

## Tests

- `tests/indicators/test_vw_ema.py` — VW-EMA correctness + NaN warmup
- `tests/indicators/test_atr.py` — Wilder seeding
- `tests/indicators/test_bands.py` — multiplier application
- `tests/indicators/test_aggregation.py` — weekly bucketing, DST, gaps
- `tests/parity/test_pine_parity.py` — end-to-end Pine numerical parity

## Performance notes

- NumPy-vectorized where it matters; per-bar Python loops where bar-by-
  bar state needs explicit handling
- Operates on lists of `OHLCV` dataclasses (not Pandas DataFrames) at
  this layer — the engine is per-ticker stateful and doesn't benefit
  from columnar storage
- For ~5000 daily bars × 1 ticker, the full indicator stack runs in
  well under 100ms
