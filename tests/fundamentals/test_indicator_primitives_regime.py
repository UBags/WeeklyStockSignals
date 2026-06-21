"""Regression tests for `compute_bar_states` post-Step-9b.

The OLD pipeline computed `core_regime` and `display_regime` via two
separate state machines and emitted both as keys on each dict in the
returned list. Step 9b dropped `compute_core_regime` and
`compute_display_regime`, replacing them with a single walk that
applies `regime.compute_regime` per bar. The dict's `display_regime`
key now carries the unified regime; `core_regime` is gone.

These tests pin:
  - the unified regime behaviour on flat/crash/rally fixtures,
  - the sticky-on-NaN warmup carry-over,
  - the absence of `core_regime` in the output schema.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from trip_ema.fundamentals.indicator_primitives import (
    INDICATOR_DEFAULTS,
    _compute_regime_series,
    compute_bar_states,
    compute_indicator_series,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _build_bar_inputs(prices: list[float]) -> tuple[pd.Series, ...]:
    """Build the (closes, volumes, highs, lows, dates) tuple for a
    weekly bar series starting 2024-01-01, prices = closes."""
    closes = pd.Series([float(p) for p in prices])
    volumes = pd.Series([1_000_000.0] * len(prices))
    highs = closes + 0.5
    lows = closes - 0.5
    dates = pd.Series(
        [date(2024, 1, 1) + timedelta(weeks=i) for i in range(len(prices))]
    )
    return closes, volumes, highs, lows, dates


def _states_for(prices: list[float]) -> list[dict]:
    closes, volumes, highs, lows, dates = _build_bar_inputs(prices)
    series = compute_indicator_series(
        closes, volumes, highs, lows, params=INDICATOR_DEFAULTS,
    )
    return compute_bar_states(
        closes, volumes, highs, lows,
        trade_dates=dates, series=series,
    )


# ===========================================================================
# Schema
# ===========================================================================


class TestSchema:

    def test_no_core_regime_key_in_output(self):
        """Step 9b dropped the core_regime dict key."""
        rows = _states_for([100.0] * 70)
        assert all("core_regime" not in r for r in rows)

    def test_display_regime_key_still_present(self):
        rows = _states_for([100.0] * 70)
        assert all("display_regime" in r for r in rows)

    def test_display_regime_values_in_minus_one_zero_one(self):
        rows = _states_for([100.0] * 70 + [80.0, 70.0, 60.0])
        for r in rows:
            assert r["display_regime"] in (-1, 0, 1), (
                f"display_regime out of range: {r['display_regime']}"
            )


# ===========================================================================
# Regime behaviour
# ===========================================================================


class TestRegimeBehaviour:

    def test_long_flat_paints_neutral_at_end(self):
        """A flat-at-100 history without trend produces NEUTRAL on
        the latest bar — close sits between the inner bands, no
        edge condition fires."""
        rows = _states_for([100.0] * 80)
        assert rows[-1]["display_regime"] == 0

    def test_sharp_crash_paints_bearish(self):
        """A sharp downward break out of the inner band paints
        BEARISH on the latest bar."""
        prices = [100.0] * 60 + list(np.linspace(95.0, 50.0, 5))
        rows = _states_for(prices)
        assert rows[-1]["display_regime"] == -1
        # And the bar BEFORE the crash should NOT be BEARISH —
        # exercising that the transition lands on the crash bars,
        # not earlier.
        assert rows[59]["display_regime"] != -1

    def test_sharp_rally_paints_bullish(self):
        """Mirror — a sharp upward break paints BULLISH."""
        prices = [100.0] * 60 + list(np.linspace(105.0, 150.0, 5))
        rows = _states_for(prices)
        assert rows[-1]["display_regime"] == 1
        assert rows[59]["display_regime"] != 1


# ===========================================================================
# Sticky-on-NaN warmup
# ===========================================================================


class TestWarmupCarryOver:

    def test_warmup_bars_keep_neutral_until_indicators_settle(self):
        """During warmup (NaN indicators), the regime stays at its
        initial NEUTRAL value rather than spuriously flipping."""
        rows = _states_for([100.0] * 20)
        # The first ~12 bars are in warmup (ATR=12 + EMA seed). They
        # should all carry display_regime=0 (NEUTRAL).
        for i, r in enumerate(rows[:8]):
            assert r["display_regime"] == 0, (
                f"bar {i} got display_regime={r['display_regime']} during warmup"
            )

    def test_compute_regime_series_returns_correct_length(self):
        closes, volumes, highs, lows, _ = _build_bar_inputs([100.0] * 30)
        series = compute_indicator_series(closes, volumes, highs, lows,
                                          params=INDICATOR_DEFAULTS)
        regime = _compute_regime_series(closes, series)
        assert len(regime) == 30

    def test_compute_regime_series_empty_input(self):
        empty = pd.Series([], dtype="float64")
        # No series object needed since the walker bails on n==0.
        from trip_ema.fundamentals.indicator_primitives import _compute_regime_series
        result = _compute_regime_series(empty, None)
        assert len(result) == 0
