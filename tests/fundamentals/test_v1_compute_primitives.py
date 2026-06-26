"""Tests for compute_state_primitives.

Unit tests of the lean V1 compute output. Verifies:
  - Output dicts have ONLY the 12 core fields + bar_timestamp
  - No enrichment fields are emitted
  - NaN-warmup bars are skipped
  - Output values match what `compute_indicator_series` would produce
    (the function is a pure wrapper — no math of its own)
  - The synthetic-ATR-for-index path works
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from trip_ema.fundamentals.indicator_primitives import (
    INDICATOR_DEFAULTS,
    _compute_regime_series,
    compute_indicator_series,
)
from trip_ema.fundamentals.v1_compute_primitives import (
    compute_state_primitives,
)


# Expected per-bar keys — pinned so a future refactor that quietly
# adds enrichment fields back into the core output is caught.
EXPECTED_KEYS = {
    "bar_timestamp",
    "med_ema", "long_ema", "longest_ema",
    "atr",
    "upper_inner_band", "lower_inner_band",
    "upper_mid_band", "lower_mid_band",
    "upper_outer_band", "lower_outer_band",
    "display_regime",
}


def _synthetic_bars(n: int) -> tuple[pd.Series, ...]:
    """N bars of a simple sine-ish trajectory good enough to exercise
    the EMA/ATR/regime walk without being all-equal (which can produce
    degenerate ATR=0 forever)."""
    close = pd.Series(
        [100.0 + 5.0 * math.sin(i / 4.0) + i * 0.1 for i in range(n)],
        dtype=float,
    )
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series([1_000_000.0] * n, dtype=float)
    return close, high, low, volume


def _dates(n: int) -> pd.Series:
    """N consecutive weekly dates."""
    start = date(2026, 1, 5)  # Monday
    return pd.Series([start + timedelta(weeks=i) for i in range(n)])


# ============================================================
# Empty input
# ============================================================

def test_empty_input_returns_empty_list():
    empty = pd.Series([], dtype=float)
    out = compute_state_primitives(
        empty, empty, empty, empty,
        trade_dates=pd.Series([], dtype="object"),
    )
    assert out == []


# ============================================================
# Output shape
# ============================================================

def test_output_dicts_have_exactly_expected_keys():
    n = 100
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)
    out = compute_state_primitives(
        close, volume, high, low, trade_dates=dates,
    )
    assert len(out) > 0, (
        "100 bars should produce at least some indicator-ready output"
    )
    for d in out:
        assert set(d.keys()) == EXPECTED_KEYS, (
            f"Per-bar dict has wrong key set. "
            f"Extra: {set(d.keys()) - EXPECTED_KEYS}, "
            f"Missing: {EXPECTED_KEYS - set(d.keys())}"
        )


def test_output_does_not_contain_enrichment_keys():
    """The point of the V1 split — no pierces flags, no pattern labels,
    no turn flags, no distance metrics in the core output."""
    n = 100
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)
    out = compute_state_primitives(
        close, volume, high, low, trade_dates=dates,
    )
    forbidden = {
        "pierces_upper_mid", "pierces_lower_mid",
        "dist_to_med_in_hl_units", "dist_to_long_in_hl_units",
        "dist_to_longest_in_hl_units",
        "mean_dist_to_med_in_hl_units", "mean_dist_to_long_in_hl_units",
        "mean_dist_to_longest_in_hl_units",
        "bars_since_med_turn", "bars_since_long_turn",
        "bars_since_longest_turn",
        "pct_change_since_med_turn", "pct_change_since_long_turn",
        "pct_change_since_longest_turn",
        "med_pattern_label", "long_pattern_label",
        "longest_pattern_label",
        "med_turn_buy", "med_turn_sell",
        "long_turn_buy", "long_turn_sell",
        "longest_turn_buy", "longest_turn_sell",
    }
    for d in out:
        for k in forbidden:
            assert k not in d, (
                f"Enrichment key {k!r} leaked into core output. "
                f"Enrichment must live in StockIndicatorEnrichment "
                f"(Step 5 of clean-pipeline V1)."
            )


# ============================================================
# Warmup behavior
# ============================================================

def test_warmup_bars_are_skipped():
    """The first few bars (before EMA/ATR stabilize) produce NaN
    indicators. The lean V1 emitter skips those — output length is
    strictly less than input length."""
    n = 100
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)
    out = compute_state_primitives(
        close, volume, high, low, trade_dates=dates,
    )
    assert len(out) < n, (
        "Warmup bars should be dropped — expected fewer output "
        "rows than input bars."
    )


def test_emitted_rows_have_no_nan_values():
    """Every emitted row must have finite numeric values — that's the
    whole point of the warmup skip. The repo's NOT NULL constraints
    would reject NaN anyway."""
    n = 100
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)
    out = compute_state_primitives(
        close, volume, high, low, trade_dates=dates,
    )
    for d in out:
        for k in (
            "med_ema", "long_ema", "longest_ema", "atr",
            "upper_inner_band", "lower_inner_band",
            "upper_mid_band", "lower_mid_band",
            "upper_outer_band", "lower_outer_band",
        ):
            assert math.isfinite(d[k]), (
                f"NaN/inf leaked into output: {k}={d[k]!r}"
            )


# ============================================================
# Value parity with the existing math layer
# ============================================================

def test_values_match_compute_indicator_series_directly():
    """compute_state_primitives is a thin wrapper — its emitted values
    must equal what compute_indicator_series + _compute_regime_series
    would produce for the same input. No re-math.
    """
    n = 100
    close, high, low, volume = _synthetic_bars(n)
    dates = _dates(n)

    # Reference computation directly via the underlying primitives.
    series = compute_indicator_series(
        close, volume, high, low, params=INDICATOR_DEFAULTS,
    )
    regime = _compute_regime_series(close, series)

    # V1 wrapper output.
    out = compute_state_primitives(
        close, volume, high, low, trade_dates=dates,
    )

    # Match each emitted row against the corresponding input index.
    out_by_dt = {d["bar_timestamp"]: d for d in out}
    for i in range(n):
        dt = dates.iloc[i]
        if dt not in out_by_dt:
            # Warmup bar — verify the underlying math IS NaN here
            # for any of the fields the V1 skip logic checks
            # (med / long / longest / atr).
            assert (
                pd.isna(series.emas.med.iloc[i])
                or pd.isna(series.emas.long.iloc[i])
                or pd.isna(series.emas.longest.iloc[i])
                or pd.isna(series.atr.iloc[i])
            ), (
                f"Bar {i} was skipped but underlying math is finite "
                f"for med/long/longest/atr — V1 wrapper is dropping "
                f"legitimate rows."
            )
            continue
        d = out_by_dt[dt]
        assert d["med_ema"] == pytest.approx(
            float(series.emas.med.iloc[i]),
        )
        assert d["long_ema"] == pytest.approx(
            float(series.emas.long.iloc[i]),
        )
        assert d["longest_ema"] == pytest.approx(
            float(series.emas.longest.iloc[i]),
        )
        assert d["atr"] == pytest.approx(float(series.atr.iloc[i]))
        assert d["upper_inner_band"] == pytest.approx(
            float(series.bands.upper_inner.iloc[i]),
        )
        assert d["display_regime"] == int(regime.iloc[i])


# ============================================================
# Synthetic-ATR-for-index path
# ============================================================

def test_synthetic_atr_for_index_produces_output():
    """Industry-index path: pass high=low=close=value, volume=1, and
    synthetic_atr_for_index=True. Output should still produce rows."""
    n = 100
    close = pd.Series(
        [1000.0 + i * 2.0 + 50.0 * math.sin(i / 6.0) for i in range(n)],
        dtype=float,
    )
    # Industry indices have synthetic OHLC where O=H=L=C.
    high = close.copy()
    low = close.copy()
    volume = pd.Series([1.0] * n, dtype=float)
    dates = _dates(n)

    out = compute_state_primitives(
        close, volume, high, low,
        trade_dates=dates,
        synthetic_atr_for_index=True,
    )
    assert len(out) > 0, (
        "Industry-index path should produce indicator-ready bars too"
    )
    # All emitted rows must still be NaN-free.
    for d in out:
        assert math.isfinite(d["atr"])
        assert math.isfinite(d["med_ema"])
