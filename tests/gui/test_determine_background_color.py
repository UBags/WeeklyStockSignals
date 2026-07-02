"""Truth table for DetermineBackgroundColor -- the single source of truth
for GUI background color. Covers both disjuncts of every rule, the
operator's Red->Green->Neutral edge case (must be YELLOW, not the old
false green), the None fall-through, and the DB conversions.
"""
import pytest

from trip_ema.gui.determine_background_color import DetermineBackgroundColor as DBC
from trip_ema.domain.state import ChangeSignal as C, CoreRegime as R

GREEN, RED, YELLOW, GRAY = DBC.GREEN, DBC.RED, DBC.YELLOW, DBC.GRAY
color = DBC.determine_background_color


class TestFirstDisjunct_CurrentBarSignal:
    """current_signal alone decides the color; regime/previous irrelevant."""

    @pytest.mark.parametrize("sig,want", [
        (C.BUY, GREEN),
        (C.SELL, RED),
        (C.EXIT_FROM_BUY, YELLOW),
        (C.EXIT_FROM_SELL, GRAY),
    ])
    def test_current_signal_wins_regardless_of_regime(self, sig, want):
        # previous_signal=None kills every second-disjunct "hold", so only
        # the current-bar signal can fire — proving the first disjunct
        # decides on its own, whatever the regime is.
        assert color(sig, R.NEUTRAL, None) is want
        assert color(sig, R.BEARISH, None) is want
        assert color(sig, R.BULLISH, None) is want


class TestSecondDisjunct_RegimeHold:
    """No flip this bar (current_signal None); color persists via
    (current_regime, previous_signal)."""

    def test_green_hold(self):
        assert color(None, R.BULLISH, C.BUY) is GREEN

    def test_red_hold(self):
        assert color(None, R.BEARISH, C.SELL) is RED

    def test_yellow_hold(self):
        assert color(None, R.NEUTRAL, C.EXIT_FROM_BUY) is YELLOW

    def test_gray_hold(self):
        assert color(None, R.NEUTRAL, C.EXIT_FROM_SELL) is GRAY


class TestEdgeCases:
    def test_red_then_green_then_neutral_is_yellow(self):
        """Operator's edge case: trajectory Red -> Green -> Neutral, and
        the current bar is Neutral. The Green->Neutral flip THIS bar is
        EXIT_FROM_BUY; the prior bar's Red->Green was BUY. The old code
        keyed off the BUY and painted green -- WRONG. Correct = YELLOW.
        """
        current_signal = C.EXIT_FROM_BUY   # Green -> Neutral this bar
        current_regime = R.NEUTRAL
        previous_signal = C.BUY            # Red -> Green, the prior bar
        assert color(current_signal, current_regime, previous_signal) is YELLOW

    def test_stale_green_no_recent_flip_is_blank(self):
        """Two-bar rule: quietly Green for months, no flip this bar and
        none last bar -> previous_signal is None -> no color."""
        assert color(None, R.BULLISH, None) is None

    def test_no_signal_history_neutral_is_blank(self):
        assert color(None, R.NEUTRAL, None) is None

    def test_regime_hold_but_wrong_previous_is_blank(self):
        """Green regime but previous_signal isn't BUY and no current
        signal -> no branch fires -> None. (Incoherent under the
        regime<->signal coupling; a safe, visible non-color.)"""
        assert color(None, R.BULLISH, C.EXIT_FROM_SELL) is None


class TestCrossEnumValueCollision:
    """ChangeSignal and CoreRegime share integer values; the `is`
    comparisons must not let a regime value satisfy a signal branch."""

    def test_regime_value_does_not_leak_into_signal_slot(self):
        # R.BULLISH == 1 == C.BUY by value, but passing a regime where a
        # signal is expected must NOT trigger the BUY branch.
        assert color(R.BULLISH, R.NEUTRAL, None) is None  # type: ignore[arg-type]


class TestDbConversions:
    @pytest.mark.parametrize("s,want", [
        ("BUY", C.BUY),
        ("SELL", C.SELL),
        ("EXIT_FROM_BUY", C.EXIT_FROM_BUY),
        ("EXIT_FROM_SELL", C.EXIT_FROM_SELL),
        (None, None),
        ("WOBBLE", None),
    ])
    def test_signal_from_db(self, s, want):
        assert DBC.signal_from_db(s) is want

    @pytest.mark.parametrize("v,want", [
        (1, R.BULLISH),
        (0, R.NEUTRAL),
        (-1, R.BEARISH),
        (None, None),
        (7, None),
    ])
    def test_regime_from_db(self, v, want):
        assert DBC.regime_from_db(v) is want
