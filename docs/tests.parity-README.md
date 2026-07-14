# Pine Parity Test Harness

This directory holds CSV fixtures and a test harness that verify the
Python indicator output bar-for-bar against expected values. The
fixtures are meant to be **eventually replaced** with real exports
from TradingView so we catch numerical drift vs. Pine Script.

## Why this exists

The Python port is faithful to Pine *by construction* — every EMA
recurrence, every NaN warmup, every Wilder seed matches Pine. But
without a parity test, a future refactor could silently introduce
a 0.001% drift that nobody notices for months. This harness exists
so that any change to indicator math has to either preserve the
fixtures or explicitly regenerate them with a documented reason.

## Current fixture state

The committed fixtures are **self-pinning** — they were generated
from the current Python implementation. They protect against
*future* drift in the Python code but do NOT verify parity with
Pine. To upgrade them to real Pine fixtures, see "Regenerating from
TradingView" below.

## Fixture format

CSV with header row:

    timestamp,open,high,low,close,volume,
    expected_med_ema,expected_long_ema,expected_longest_ema,expected_atr

- `timestamp` is ISO-8601 UTC.
- OHLCV are the input bars.
- The four `expected_*` columns hold the bar-state outputs.
- Empty cells in `expected_*` mean NaN (warmup phase).

## How the harness uses fixtures

`test_pine_parity.py` discovers all `*.csv` files in this directory,
parameterises a single test over them, and for each fixture:

1. Creates a `TickerEngine` with the default `IndicatorParams`.
2. Feeds the OHLCV bars one at a time via `ingest()`.
3. After every bar, compares the engine's `latest_state()` against
   the expected row.
4. NaN-vs-NaN matches; finite-vs-finite compares with absolute
   tolerance `1e-9` (numpy/IEEE float jitter is the only source of
   sub-`1e-12` differences in this code).

## Regenerating from TradingView

When you have TradingView Premium and can export indicator values:

1. Open the Weekly_Triple_EMA Pine script in TradingView for the
   target instrument and timeframe.
2. Use the "Bar Replay" feature or the chart's data window to
   collect rows of `(timestamp, OHLCV, med_ema, long_ema,
   longest_ema, atr)`.
3. Save as `tradingview_<symbol>_<timeframe>.csv` in this
   directory with the columns above.
4. Run `pytest tests/parity -v`. Any drift will surface as a
   numerical mismatch on a specific bar.

If a fixture fails AFTER a deliberate change to indicator math, the
fix is to re-export from TradingView (not to relax the tolerance).
This is the contract.

## Tolerance choice

`1e-9` absolute tolerance accommodates Python's double-precision
float jitter (`~2.2e-16` per op × ~5000 ops at typical history depth
≈ `1.1e-12`). It does NOT accommodate algorithmic differences. If
parity is failing at the `1e-9` level, the math has drifted.

Pine's runtime uses the same IEEE-754 doubles as CPython, so true
parity is achievable. Differences seen in practice usually come from:

- Different bar boundaries (DST, session hours)
- Different volume-smoothing window
- Different warmup-NaN-propagation rule

All three are pinned by our other tests; this harness adds the
end-to-end numerical check.
