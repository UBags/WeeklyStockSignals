"""A short return is bounded at +100%. The price can only fall to zero.

realized_ret for a short used to be entry_open / exit_close - 1.0, which
divides by the EXIT price and diverges as that approaches zero. On run 69 it
reported TVVISION (282 -> 1.35) as +20,790% and put 1,514 short labels above a
100% gain, contaminating capture_ratio, excess_ret_vs_industry,
excess_ret_vs_market and anything trained on the column.

These tests pin the bound and the internal consistency that the old formula
broke: every short metric in walk_signal denominates in entry_open.
"""
from __future__ import annotations

import pytest

from trip_ema.domain.forward_walk import LONG, SHORT, walk_signal


class _B:
    __slots__ = ("open", "high", "low", "close")

    def __init__(self, o, h, low, c):
        self.open, self.high, self.low, self.close = o, h, low, c


def test_a_short_gain_never_exceeds_one_hundred_percent():
    """TVVISION's real shape: 282 -> 1.35 over the window."""
    bars = [_B(282, 282, 280, 281), _B(50, 50, 1.30, 1.35)]
    r = walk_signal(282.0, bars, SHORT)
    assert r.realized_ret < 1.0
    assert r.realized_ret == pytest.approx((282 - 1.35) / 282)
    assert r.realized_ret == pytest.approx(0.99521, abs=1e-5)


def test_a_price_collapsing_toward_zero_approaches_but_never_reaches_one():
    for exit_close in (10.0, 1.0, 0.1, 0.01, 0.0001):
        bars = [_B(100, 100, 99, 100), _B(exit_close, exit_close,
                                          exit_close, exit_close)]
        r = walk_signal(100.0, bars, SHORT)
        assert 0.0 < r.realized_ret < 1.0, (exit_close, r.realized_ret)


def test_short_and_long_are_denominated_the_same_way():
    """The bug was one denominator for realized_ret and another for MFE."""
    bars = [_B(100, 101, 98, 99), _B(99, 100, 94, 95), _B(95, 97, 93, 96)]
    r = walk_signal(100.0, bars, SHORT)
    assert r.realized_ret == pytest.approx((100 - 96) / 100)
    # MFE for a short is (entry_open - low) / entry_open: low of 93 -> 7%.
    assert r.mfe_pct == pytest.approx(0.07)


def test_capture_ratio_stays_sane_on_a_collapse():
    """capture_ratio = realized_ret / mfe_pct. Mixing denominators scored
    TVVISION at ~209; it belongs at or below 1.0."""
    bars = [_B(282, 282, 280, 281), _B(50, 50, 1.30, 1.35)]
    r = walk_signal(282.0, bars, SHORT)
    assert r.capture_ratio is not None
    assert 0.0 < r.capture_ratio <= 1.0, r.capture_ratio


def test_a_losing_short_is_negative_and_unbounded_below():
    """The asymmetry is real: a short's LOSS is unbounded, its gain is not."""
    bars = [_B(100, 100, 99, 100), _B(400, 400, 400, 400)]
    r = walk_signal(100.0, bars, SHORT)
    assert r.realized_ret == pytest.approx(-3.0)


def test_long_returns_are_untouched():
    bars = [_B(100, 101, 99, 100), _B(100, 110, 100, 110)]
    r = walk_signal(100.0, bars, LONG)
    assert r.realized_ret == pytest.approx(0.10)


def test_a_flat_short_returns_zero():
    bars = [_B(100, 101, 99, 100), _B(100, 100, 100, 100)]
    assert walk_signal(100.0, bars, SHORT).realized_ret == pytest.approx(0.0)
