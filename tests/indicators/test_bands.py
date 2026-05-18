"""Tests for compute_bands and pierce detection."""

from __future__ import annotations

import pytest

from trip_ema.domain import IndicatorParams
from trip_ema.indicators.bands import (
    compute_bands,
    detects_upper_mid_pierce,
    detects_lower_mid_pierce,
)


def _params(inner: float = 0.2, mid: float = 2.0, outer: float = 4.0) -> IndicatorParams:
    return IndicatorParams(inner_mult=inner, mid_mult=mid, outer_mult=outer)


def test_bands_use_highest_and_lowest_of_three_emas():
    # Three EMAs: 100, 105, 95. Highest=105, lowest=95.
    # ATR = 10. Inner=0.2 -> 2. Mid=2.0 -> 20. Outer=4.0 -> 40.
    bands = compute_bands(100.0, 105.0, 95.0, atr=10.0, params=_params())
    assert bands.upper_inner == pytest.approx(105 + 2)
    assert bands.lower_inner == pytest.approx(95 - 2)
    assert bands.upper_mid == pytest.approx(105 + 20)
    assert bands.lower_mid == pytest.approx(95 - 20)
    assert bands.upper_outer == pytest.approx(105 + 40)
    assert bands.lower_outer == pytest.approx(95 - 40)


def test_bands_order_is_consistent():
    """Upper levels strictly above the ribbon; lower levels strictly below.
    Outer further from ribbon than mid, mid further than inner."""
    bands = compute_bands(100, 105, 95, atr=10.0, params=_params())
    assert bands.lower_outer < bands.lower_mid < bands.lower_inner < 95
    assert 105 < bands.upper_inner < bands.upper_mid < bands.upper_outer


def test_zero_atr_collapses_bands_to_emas():
    bands = compute_bands(100, 105, 95, atr=0.0, params=_params())
    assert bands.upper_inner == 105
    assert bands.lower_inner == 95


def test_pierce_when_bar_straddles_level():
    # Bar: low=100, high=120. Level at 110 is pierced.
    assert detects_upper_mid_pierce(high=120, low=100, upper_mid=110) is True


def test_no_pierce_when_bar_entirely_above_level():
    assert detects_upper_mid_pierce(high=130, low=120, upper_mid=110) is False


def test_no_pierce_when_bar_entirely_below_level():
    assert detects_upper_mid_pierce(high=109, low=100, upper_mid=110) is False


def test_pierce_when_level_equals_high():
    """Boundary: high == level should count as a pierce."""
    assert detects_upper_mid_pierce(high=110, low=100, upper_mid=110) is True


def test_pierce_when_level_equals_low():
    """Boundary: low == level should count as a pierce."""
    assert detects_upper_mid_pierce(high=120, low=110, upper_mid=110) is True


def test_lower_pierce_symmetric_to_upper():
    assert detects_lower_mid_pierce(high=110, low=90, lower_mid=100) is True
    assert detects_lower_mid_pierce(high=110, low=105, lower_mid=100) is False
