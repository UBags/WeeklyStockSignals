"""Tests for the display regime gating layer.

The display regime is what actually gets rendered. The two gates
(green_gate and red_gate) differ between fresh entry (from NEUTRAL) and
continuation. Tests verify every cell of the transition table.
"""

from __future__ import annotations

from trip_ema.domain import CoreRegime
from trip_ema.engine.display_regime import (
    DisplayRegimeInputs,
    step_display_regime,
)


def _i(
    *,
    open: float = 100.0,
    close: float = 100.0,
    upper_inner: float = 110.0,
    lower_inner: float = 90.0,
    upper_outer: float = 130.0,
    lower_outer: float = 70.0,
) -> DisplayRegimeInputs:
    return DisplayRegimeInputs(
        open=open, close=close,
        upper_inner=upper_inner, lower_inner=lower_inner,
        upper_outer=upper_outer, lower_outer=lower_outer,
    )


# -------------------- Core==NEUTRAL forces display==NEUTRAL -------------------- #

def test_neutral_core_forces_neutral_display_even_from_bullish():
    """When the core regime drops to neutral, display must follow regardless
    of the previous display state."""
    inputs = _i(open=100, close=120)   # strong green candle
    assert step_display_regime(
        CoreRegime.BULLISH, CoreRegime.NEUTRAL, inputs
    ) == CoreRegime.NEUTRAL


def test_neutral_core_forces_neutral_display_even_from_bearish():
    inputs = _i(open=100, close=80)
    assert step_display_regime(
        CoreRegime.BEARISH, CoreRegime.NEUTRAL, inputs
    ) == CoreRegime.NEUTRAL


# -------------------- Fresh entry: NEUTRAL → BULLISH -------------------- #

class TestFreshEntryToBullish:
    def test_passes_when_green_candle_inside_corridor(self):
        # close past upper_inner, below upper_outer, green candle
        inputs = _i(open=105, close=115, upper_inner=110, upper_outer=130)
        result = step_display_regime(CoreRegime.NEUTRAL, CoreRegime.BULLISH, inputs)
        assert result == CoreRegime.BULLISH

    def test_blocked_when_red_candle(self):
        # close past upper_inner but candle is red
        inputs = _i(open=120, close=115, upper_inner=110, upper_outer=130)
        result = step_display_regime(CoreRegime.NEUTRAL, CoreRegime.BULLISH, inputs)
        assert result == CoreRegime.NEUTRAL

    def test_blocked_when_close_not_past_upper_inner(self):
        inputs = _i(open=100, close=109, upper_inner=110, upper_outer=130)
        result = step_display_regime(CoreRegime.NEUTRAL, CoreRegime.BULLISH, inputs)
        assert result == CoreRegime.NEUTRAL

    def test_blocked_when_close_above_outer_corridor(self):
        """The Goldilocks gate: fresh entry rejected if close is too far past
        the outer level. This is the 'big candle missed' case we discussed
        earlier — by design here, we're a faithful Pine port."""
        inputs = _i(open=100, close=135, upper_inner=110, upper_outer=130)
        result = step_display_regime(CoreRegime.NEUTRAL, CoreRegime.BULLISH, inputs)
        assert result == CoreRegime.NEUTRAL


# -------------------- Continuation: BULLISH → BULLISH -------------------- #

class TestContinuationBullish:
    def test_continues_when_green_candle_past_upper_inner_even_if_past_outer(self):
        """In continuation (not fresh entry), the outer corridor check is
        skipped. A runaway green candle still paints."""
        inputs = _i(open=120, close=140, upper_inner=110, upper_outer=130)
        result = step_display_regime(CoreRegime.BULLISH, CoreRegime.BULLISH, inputs)
        assert result == CoreRegime.BULLISH

    def test_holds_when_gate_fails(self):
        """Red candle while in bullish display: gate fails but we hold the
        previous bullish state (not auto-unpaint)."""
        inputs = _i(open=120, close=115, upper_inner=110)
        result = step_display_regime(CoreRegime.BULLISH, CoreRegime.BULLISH, inputs)
        assert result == CoreRegime.BULLISH


# -------------------- Flip: BEARISH → BULLISH -------------------- #

def test_flip_from_bearish_uses_loose_gate():
    """Flipping from bearish to bullish: outer corridor check is skipped
    (only fresh entries from NEUTRAL require it)."""
    inputs = _i(open=100, close=140, upper_inner=110, upper_outer=130)
    result = step_display_regime(CoreRegime.BEARISH, CoreRegime.BULLISH, inputs)
    assert result == CoreRegime.BULLISH


# -------------------- Symmetric tests for BEARISH side -------------------- #

class TestFreshEntryToBearish:
    def test_passes_when_red_candle_inside_corridor(self):
        inputs = _i(open=95, close=85, lower_inner=90, lower_outer=70)
        result = step_display_regime(CoreRegime.NEUTRAL, CoreRegime.BEARISH, inputs)
        assert result == CoreRegime.BEARISH

    def test_blocked_when_close_below_outer(self):
        inputs = _i(open=100, close=65, lower_inner=90, lower_outer=70)
        result = step_display_regime(CoreRegime.NEUTRAL, CoreRegime.BEARISH, inputs)
        assert result == CoreRegime.NEUTRAL


# -------------------- Hold-on-gate-fail -------------------- #

def test_hold_does_not_apply_when_previous_display_was_opposite():
    """If previous display was BEARISH and core wants BULLISH but gate fails,
    we should NOT hold the previous bearish display."""
    inputs = _i(open=120, close=115, upper_inner=110)  # red candle
    result = step_display_regime(CoreRegime.BEARISH, CoreRegime.BULLISH, inputs)
    # Red candle fails green gate; previous was BEARISH (not BULLISH),
    # so we don't hold. Falls through to NEUTRAL.
    assert result == CoreRegime.NEUTRAL


# -------------------- Goldilocks corridor flag -------------------- #

class TestGoldilocksCorridorFlag:
    """The flag controls whether the outer-band check applies on fresh
    entries. Default is True (faithful Pine behavior). False allows
    explosive breakouts to paint."""

    def test_default_true_blocks_explosive_fresh_entry_to_bullish(self):
        # close past outer band on a fresh entry from NEUTRAL → blocked
        inputs = _i(open=100, close=135, upper_inner=110, upper_outer=130)
        result = step_display_regime(
            CoreRegime.NEUTRAL, CoreRegime.BULLISH, inputs,
            goldilocks_corridor_on_fresh_entry_only=True,
        )
        assert result == CoreRegime.NEUTRAL

    def test_flag_false_allows_explosive_fresh_entry_to_bullish(self):
        # Same setup; flag disabled → paints BULLISH
        inputs = _i(open=100, close=135, upper_inner=110, upper_outer=130)
        result = step_display_regime(
            CoreRegime.NEUTRAL, CoreRegime.BULLISH, inputs,
            goldilocks_corridor_on_fresh_entry_only=False,
        )
        assert result == CoreRegime.BULLISH

    def test_flag_does_not_affect_continuation(self):
        """Continuation (from BULLISH to BULLISH) was never gated by outer
        band, regardless of flag value."""
        inputs = _i(open=120, close=140, upper_inner=110, upper_outer=130)
        for flag in (True, False):
            result = step_display_regime(
                CoreRegime.BULLISH, CoreRegime.BULLISH, inputs,
                goldilocks_corridor_on_fresh_entry_only=flag,
            )
            assert result == CoreRegime.BULLISH, f"flag={flag}"

    def test_flag_does_not_affect_flip_from_opposite(self):
        """Flip from BEARISH to BULLISH was never gated by outer band."""
        inputs = _i(open=100, close=140, upper_inner=110, upper_outer=130)
        for flag in (True, False):
            result = step_display_regime(
                CoreRegime.BEARISH, CoreRegime.BULLISH, inputs,
                goldilocks_corridor_on_fresh_entry_only=flag,
            )
            assert result == CoreRegime.BULLISH, f"flag={flag}"

    def test_flag_false_allows_explosive_fresh_entry_to_bearish(self):
        """Symmetric to bullish — explosive drops also paint when flag is off."""
        inputs = _i(open=100, close=65, lower_inner=90, lower_outer=70)
        result = step_display_regime(
            CoreRegime.NEUTRAL, CoreRegime.BEARISH, inputs,
            goldilocks_corridor_on_fresh_entry_only=False,
        )
        assert result == CoreRegime.BEARISH
