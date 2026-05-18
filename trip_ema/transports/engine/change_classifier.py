"""Change classifier — turn a display-regime transition into a ChangeSignal.

This is the rule used both by the engine (for the per-bar `latest_change`
accessor) and by the ComputeScheduler (for detecting cross-compute change
events that fire notifications).

The taxonomy:

    NO_CHANGE     : display unchanged
    BUY           : display -> BULLISH (from any other state)
    SELL          : display -> BEARISH (from any other state)
    EXIT_FROM_BUY : display: BULLISH -> NEUTRAL
    EXIT_FROM_SELL: display: BEARISH -> NEUTRAL
    (BULLISH <-> BEARISH direct flips are classified as BUY or SELL —
     no separate "flip" signal type; the move into the new state is what
     matters for the notification.)
"""

from __future__ import annotations

from ..domain import ChangeSignal, CoreRegime


def classify_change(previous: CoreRegime, current: CoreRegime) -> ChangeSignal:
    if previous == current:
        return ChangeSignal.NO_CHANGE
    if current == CoreRegime.BULLISH:
        return ChangeSignal.BUY
    if current == CoreRegime.BEARISH:
        return ChangeSignal.SELL
    # current == NEUTRAL, previous was non-neutral
    if previous == CoreRegime.BULLISH:
        return ChangeSignal.EXIT_FROM_BUY
    if previous == CoreRegime.BEARISH:
        return ChangeSignal.EXIT_FROM_SELL
    # Unreachable — both previous and current are NEUTRAL, caught above
    return ChangeSignal.NO_CHANGE  # pragma: no cover
