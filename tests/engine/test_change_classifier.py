"""Tests for the change classifier — the 9-cell transition matrix."""

import pytest

from trip_ema.domain import ChangeSignal, CoreRegime
from trip_ema.engine.change_classifier import classify_change


# (previous, current) -> expected ChangeSignal
EXPECTED = {
    (CoreRegime.NEUTRAL, CoreRegime.NEUTRAL): ChangeSignal.NO_CHANGE,
    (CoreRegime.BULLISH, CoreRegime.BULLISH): ChangeSignal.NO_CHANGE,
    (CoreRegime.BEARISH, CoreRegime.BEARISH): ChangeSignal.NO_CHANGE,
    (CoreRegime.NEUTRAL, CoreRegime.BULLISH): ChangeSignal.BUY,
    (CoreRegime.BEARISH, CoreRegime.BULLISH): ChangeSignal.BUY,    # direct flip
    (CoreRegime.NEUTRAL, CoreRegime.BEARISH): ChangeSignal.SELL,
    (CoreRegime.BULLISH, CoreRegime.BEARISH): ChangeSignal.SELL,   # direct flip
    (CoreRegime.BULLISH, CoreRegime.NEUTRAL): ChangeSignal.EXIT_FROM_BUY,
    (CoreRegime.BEARISH, CoreRegime.NEUTRAL): ChangeSignal.EXIT_FROM_SELL,
}


@pytest.mark.parametrize("transition,expected", list(EXPECTED.items()))
def test_classify_change_matrix(transition, expected):
    previous, current = transition
    assert classify_change(previous, current) == expected
