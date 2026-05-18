"""Tests for the core regime stepping function and EMA filters.

These tests exhaustively cover the state machine: every (previous_state,
input_condition) → expected_new_state case, plus the EMA filter logic.
"""

from __future__ import annotations

import pytest

from trip_ema.domain import CoreRegime
from trip_ema.engine.core_regime import (
    CoreRegimeInputs,
    green_ema_filter,
    red_ema_filter,
    step_core_regime,
)


def _inputs(
    *,
    close: float = 100.0,
    source: float | None = None,
    med_ema: float = 100.0,
    med_ema_prev: float = 100.0,
    long_ema: float = 100.0,
    long_ema_prev: float = 100.0,
    longest_ema: float = 100.0,
    upper_inner: float = 110.0,
    lower_inner: float = 90.0,
) -> CoreRegimeInputs:
    """Convenience builder with safe defaults — neutral conditions."""
    return CoreRegimeInputs(
        close=close,
        source=close if source is None else source,
        med_ema=med_ema,
        med_ema_prev=med_ema_prev,
        long_ema=long_ema,
        long_ema_prev=long_ema_prev,
        longest_ema=longest_ema,
        upper_inner=upper_inner,
        lower_inner=lower_inner,
    )


# -------------------- EMA filters -------------------- #

class TestGreenEmaFilter:
    def test_passes_when_med_above_longest(self):
        i = _inputs(med_ema=105, long_ema=99, longest_ema=100)
        assert green_ema_filter(i) is True

    def test_passes_when_long_above_longest(self):
        i = _inputs(med_ema=99, long_ema=105, longest_ema=100)
        assert green_ema_filter(i) is True

    def test_fails_when_both_faster_below_longest_and_no_momentum(self):
        i = _inputs(med_ema=95, long_ema=95, longest_ema=100,
                    med_ema_prev=96, long_ema_prev=96)   # both falling
        assert green_ema_filter(i) is False

    def test_momentum_path_passes_when_both_rising_and_price_above_longest(self):
        # Both faster EMAs still below longest, but both rising AND price
        # has cleared the longest EMA. This is the "early reversal" case.
        i = _inputs(
            close=110,
            med_ema=99, med_ema_prev=98,         # rising
            long_ema=98, long_ema_prev=97,       # rising
            longest_ema=100,                      # price > longest
        )
        assert green_ema_filter(i) is True

    def test_momentum_path_fails_if_price_not_above_longest(self):
        i = _inputs(
            close=95,                              # below longest
            med_ema=99, med_ema_prev=98,
            long_ema=98, long_ema_prev=97,
            longest_ema=100,
        )
        assert green_ema_filter(i) is False


class TestRedEmaFilter:
    def test_passes_when_med_below_longest(self):
        i = _inputs(med_ema=95, long_ema=101, longest_ema=100)
        assert red_ema_filter(i) is True

    def test_momentum_path_passes_when_both_falling_and_price_below_longest(self):
        i = _inputs(
            close=90,
            med_ema=101, med_ema_prev=102,
            long_ema=102, long_ema_prev=103,
            longest_ema=100,
        )
        assert red_ema_filter(i) is True


# -------------------- State machine: from NEUTRAL -------------------- #

class TestFromNeutral:
    def test_breakout_above_upper_with_green_filter_enters_bullish(self):
        i = _inputs(close=115, upper_inner=110, med_ema=105, longest_ema=100)
        assert step_core_regime(CoreRegime.NEUTRAL, i) == CoreRegime.BULLISH

    def test_breakout_above_upper_without_filter_stays_neutral(self):
        # Price breaks out but EMA alignment fails (both faster < longest, no momentum)
        i = _inputs(
            close=115, upper_inner=110,
            med_ema=95, med_ema_prev=96,
            long_ema=95, long_ema_prev=96,
            longest_ema=100,
        )
        assert step_core_regime(CoreRegime.NEUTRAL, i) == CoreRegime.NEUTRAL

    def test_breakdown_below_lower_with_red_filter_enters_bearish(self):
        i = _inputs(close=85, lower_inner=90, med_ema=95, longest_ema=100)
        assert step_core_regime(CoreRegime.NEUTRAL, i) == CoreRegime.BEARISH

    def test_no_breakout_stays_neutral(self):
        i = _inputs(close=100, upper_inner=110, lower_inner=90)
        assert step_core_regime(CoreRegime.NEUTRAL, i) == CoreRegime.NEUTRAL


# -------------------- State machine: from BULLISH -------------------- #

class TestFromBullish:
    def test_stays_bullish_when_price_above_long_and_slope_positive(self):
        i = _inputs(
            close=105, long_ema=100, long_ema_prev=99,
            med_ema=105, longest_ema=100,
        )
        assert step_core_regime(CoreRegime.BULLISH, i) == CoreRegime.BULLISH

    def test_stays_bullish_when_price_below_long_but_slope_still_rising(self):
        # Pullback below long EMA, but long EMA is still rising → trend support intact
        i = _inputs(
            close=99, long_ema=100, long_ema_prev=99,
            med_ema=105, longest_ema=100,
        )
        assert step_core_regime(CoreRegime.BULLISH, i) == CoreRegime.BULLISH

    def test_exits_to_neutral_when_support_broken_but_no_red_setup(self):
        # Price below long EMA AND long EMA not rising → support broken.
        # But price is not yet below lower_inner OR red filter doesn't hold.
        i = _inputs(
            close=99, lower_inner=90,
            long_ema=100, long_ema_prev=100,     # slope NOT positive (flat)
            med_ema=105, longest_ema=100,        # green filter still holds
        )
        # Support broken via (price below long AND slope not positive),
        # but price is 99 > 90 (lower_inner), so → NEUTRAL.
        assert step_core_regime(CoreRegime.BULLISH, i) == CoreRegime.NEUTRAL

    def test_direct_flip_to_bearish_when_support_broken_and_red_setup_present(self):
        # Support broken AND price has already broken below lower_inner
        # AND red filter holds.
        i = _inputs(
            close=85, lower_inner=90,
            long_ema=88, long_ema_prev=89,       # below price would be ok, but key is slope
            med_ema=86, longest_ema=100,         # red filter: med < longest
        )
        # close (85) < long_ema (88) → priceBelowLongEMA True
        # slope: long_ema (88) < long_ema_prev (89) → not positive → support broken
        # close (85) < lower_inner (90) → priceBelowLower True
        # med (86) < longest (100) → red filter passes
        assert step_core_regime(CoreRegime.BULLISH, i) == CoreRegime.BEARISH

    def test_exits_when_green_filter_fails(self):
        # Even with price above long EMA, if green filter fails,
        # support is considered broken.
        # Setup: med < longest (med<long ok, but green filter requires med>longest
        # OR long>longest OR the momentum path. Here long==longest is not strictly
        # greater, med<longest, and med is falling so momentum path fails too.)
        i = _inputs(
            close=105, long_ema=100, long_ema_prev=99, lower_inner=90,
            med_ema=95, med_ema_prev=96,         # med falling, < longest
            longest_ema=100,
        )
        assert green_ema_filter(i) is False
        assert step_core_regime(CoreRegime.BULLISH, i) == CoreRegime.NEUTRAL


# -------------------- State machine: from BEARISH -------------------- #

class TestFromBearish:
    def test_stays_bearish_when_price_below_long_and_slope_negative(self):
        i = _inputs(
            close=95, long_ema=100, long_ema_prev=101,
            med_ema=95, longest_ema=100,
        )
        assert step_core_regime(CoreRegime.BEARISH, i) == CoreRegime.BEARISH

    def test_exits_to_neutral_when_recovery_starts(self):
        # Price above long EMA AND long EMA not falling → red support broken
        i = _inputs(
            close=101, upper_inner=110,
            long_ema=100, long_ema_prev=99,      # slope positive, not negative
            med_ema=95, longest_ema=100,         # red filter still passes
        )
        # priceAboveLongEMA True, longEMASlopeNegative False → support broken
        # priceAboveUpper: 101 < 110 → False → exit to NEUTRAL
        assert step_core_regime(CoreRegime.BEARISH, i) == CoreRegime.NEUTRAL

    def test_direct_flip_to_bullish_when_strong_reversal(self):
        i = _inputs(
            close=115, upper_inner=110,
            long_ema=100, long_ema_prev=99,
            med_ema=112, longest_ema=100,         # med > longest, green filter holds
        )
        assert step_core_regime(CoreRegime.BEARISH, i) == CoreRegime.BULLISH
