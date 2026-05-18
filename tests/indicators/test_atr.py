"""Tests for Wilder ATR."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from trip_ema.domain import OHLCVBar
from trip_ema.indicators.atr import ATR, compute_series


def _bar(o: float, h: float, l: float, c: float, v: float = 1000.0) -> OHLCVBar:
    return OHLCVBar(
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c, volume=v,
    )


# -------------------------- Validation -------------------------- #

def test_length_below_one_rejected():
    with pytest.raises(ValueError):
        ATR(length=0)


# -------------------------- Warmup -------------------------- #

def test_warmup_returns_nan_until_length_bars_seen():
    atr = ATR(length=3)
    assert math.isnan(atr.update(110, 100, 105))
    assert math.isnan(atr.update(115, 105, 110))
    # Third bar: seed completes, real value returned
    val = atr.update(120, 110, 115)
    assert not math.isnan(val)


def test_seed_is_average_of_first_n_true_ranges():
    """Verify the Wilder seed: ATR after exactly `length` bars equals the
    arithmetic mean of the first `length` true-range values."""
    # Bar 1: TR = H - L = 10
    # Bar 2: prev_close = 105, TR = max(10, |115-105|, |105-105|) = 10
    # Bar 3: prev_close = 110, TR = max(10, |120-110|, |110-110|) = 10
    atr = ATR(length=3)
    atr.update(110, 100, 105)
    atr.update(115, 105, 110)
    seed = atr.update(120, 110, 115)
    assert seed == pytest.approx(10.0)


# -------------------------- Recurrence -------------------------- #

def test_wilder_recurrence_one_step():
    atr = ATR(length=3)
    # Warmup bars all have TR = 10
    atr.update(110, 100, 105)
    atr.update(115, 105, 110)
    seed = atr.update(120, 110, 115)  # seed = 10
    # Next bar: high=130, low=110, prev_close=115
    # TR = max(130-110, |130-115|, |110-115|) = max(20, 15, 5) = 20
    # Expected: ((3-1)*10 + 20) / 3 = 40/3
    val = atr.update(130, 110, 110)
    assert val == pytest.approx(40.0 / 3.0)


def test_constant_input_keeps_atr_constant():
    """If every bar has identical OHLC, true range never changes, so ATR
    stays at its seed value forever."""
    atr = ATR(length=5)
    for _ in range(50):
        atr.update(110, 100, 105)
    # Bar 1: TR = 10. Bars 2..N: prev_close = 105, TR = max(10, |110-105|, |100-105|) = 10.
    # So every TR = 10, and ATR is always 10.
    final = atr.update(110, 100, 105)
    assert final == pytest.approx(10.0)


# -------------------------- Incremental vs batch -------------------------- #

def test_batch_matches_incremental():
    bars = [_bar(100 + i, 110 + i, 95 + i, 102 + i) for i in range(20)]
    batch = compute_series(bars, length=5)
    inc_atr = ATR(length=5)
    incremental = [inc_atr.update(b.high, b.low, b.close) for b in bars]
    for i, (b, inc) in enumerate(zip(batch, incremental)):
        if math.isnan(b) and math.isnan(inc):
            continue
        assert b == pytest.approx(inc, rel=1e-12), f"bar {i}: batch={b} inc={inc}"


# ============================================================
# Phase 9 — B3 ATR audit: confirm parity with Pine's ta.atr / ta.rma
#
# B3 audit conclusion: Pine's `ta.atr(length)` is `ta.rma(true_range, length)`,
# where `ta.rma` (Wilder's smoothing) returns NaN for the first `length-1`
# bars, then seeds from `sma(tr[0:length])`, then applies the recurrence
# `rma[i] = (rma[i-1] * (length-1) + x[i]) / length`. The Python `ATR`
# class implements exactly this contract. No fix was needed.
#
# This test pins the Pine-fidelity contract so future refactors can't
# silently drift.
# ============================================================


def test_pine_fidelity_atr_returns_nan_for_first_n_minus_one_bars():
    """Pine's ta.atr(length) returns NaN for bars 1..(length-1) and a
    real value at bar `length`. The Python port must match."""
    length = 14
    atr = ATR(length=length)
    results = []
    for i in range(length + 5):
        results.append(atr.update(110.0, 100.0, 105.0))
    # Bars 1..(length-1) must be NaN
    for i in range(length - 1):
        assert math.isnan(results[i]), (
            f"Pine ta.atr({length}) is NaN for bars 1..{length-1}; "
            f"bar {i+1} returned {results[i]}"
        )
    # Bar `length` is the first real value
    assert not math.isnan(results[length - 1]), (
        f"Pine ta.atr({length}) produces its first real value at bar "
        f"{length}; got NaN"
    )


def test_pine_fidelity_atr_seed_is_sma_of_first_length_true_ranges():
    """Pine's ta.rma seeds from sma of the first `length` samples. Our
    ATR seed must match that exactly."""
    length = 5
    atr = ATR(length=length)
    # Carefully constructed TRs: 10, 12, 14, 16, 18 → mean 14
    bars_def = [
        (110.0, 100.0, 105.0),  # bar 1: TR = high-low = 10
        (117.0, 105.0, 110.0),  # bar 2: prev_close=105, TR = max(12, |117-105|=12, |105-105|=0) = 12
        (124.0, 110.0, 115.0),  # bar 3: prev_close=110, TR = max(14, 14, 0) = 14
        (131.0, 115.0, 120.0),  # bar 4: prev_close=115, TR = max(16, 16, 0) = 16
        (138.0, 120.0, 125.0),  # bar 5: prev_close=120, TR = max(18, 18, 0) = 18
    ]
    for h, l, c in bars_def:
        last_val = atr.update(h, l, c)
    # SMA seed = (10+12+14+16+18) / 5 = 14
    assert last_val == pytest.approx(14.0)
