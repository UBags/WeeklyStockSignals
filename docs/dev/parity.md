# Pine parity harness

`tests/parity/` auto-discovers `*.csv` fixtures in
`tests/parity/fixtures/` and asserts the Python output matches
expected indicator values bar-by-bar with `1e-9` absolute
tolerance.

See ARCHITECTURE.md §8.5 for the full workflow.

## Why it exists

The Python port has to match TradingView's Pine Script
implementation **exactly**. A drift of `1e-6` in an EMA cascades
into very different BUY/SELL signal timing — that's not acceptable.

## What's pinned today

The committed fixture is a self-pinned synthetic dataset generated
from the current Python implementation. It catches Python-side
drift (someone changes an indicator formula by mistake) but does
**not** catch Pine-side drift (the Pine script behaving differently
than the Python code from day one).

## How to add a real TradingView fixture

You need a TradingView Premium account (the data window export
isn't available on the free tier).

1. Open the Weekly_Triple_EMA Pine script on TradingView for the
   ticker + timeframe you want
2. Export ~100 rows of OHLCV + indicator values via the data
   window. Columns:
   `timestamp,open,high,low,close,volume,expected_med_ema,expected_long_ema,expected_longest_ema,expected_atr`
3. Save as `tradingview_<symbol>_<timeframe>.csv` in
   `tests/parity/fixtures/`
4. Run `pytest tests/parity`. Any drift surfaces with the bar
   timestamp and column

## The `--regenerate` workflow

If you've deliberately changed indicator math AND updated the real
TV fixtures to match, regenerate the synthetic self-pin:

```bash
python -m tests.parity.regenerate
```

Order matters: TV fixtures are the source of truth; the self-pin
is a fast regression net. Regenerating the self-pin without first
confirming the TV fixtures match is how drift sneaks in unnoticed.
