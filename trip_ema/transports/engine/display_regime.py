"""Display regime — port of Pine's `display_background` gate layer.

The display layer takes the core regime and decides whether to actually
"paint" that color. The gates are stricter on fresh entries (must be in
the Goldilocks corridor — past inner band but not past outer) and looser
on continuations / flips from the opposite color.

The Pine logic, refactored as we did in the indicator:

    is_fresh_entry = prev_display == NEUTRAL

    green_gate = candleIsGreen
                 AND close > upper_inner
                 AND (not is_fresh_entry OR close < upper_outer)

    red_gate = candleIsRed
               AND close < lower_inner
               AND (not is_fresh_entry OR close > lower_outer)

    if core == BULLISH:
        display = BULLISH if green_gate else (BULLISH if prev_display == BULLISH else NEUTRAL)
    elif core == BEARISH:
        display = BEARISH if red_gate   else (BEARISH if prev_display == BEARISH else NEUTRAL)
    else:
        display = NEUTRAL
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import CoreRegime


@dataclass(frozen=True, slots=True)
class DisplayRegimeInputs:
    """Everything the display stepping function needs for one bar."""

    open: float
    close: float
    upper_inner: float
    lower_inner: float
    upper_outer: float
    lower_outer: float


def step_display_regime(
    previous_display: CoreRegime,
    current_core: CoreRegime,
    inputs: DisplayRegimeInputs,
    *,
    goldilocks_corridor_on_fresh_entry_only: bool = True,
) -> CoreRegime:
    """Pure function. Given the previous display state, the current core
    regime, and the current bar's inputs, return the new display state.

    Args:
        previous_display: The display regime from the previous bar.
        current_core: The core regime computed for the current bar.
        inputs: OHLC + the six ATR bands for the current bar.
        goldilocks_corridor_on_fresh_entry_only: When True (default),
            preserves the faithful Pine behavior — fresh entries from
            NEUTRAL must have close inside the Goldilocks corridor (past
            inner band, before outer band). When False, the outer-band
            check is dropped on fresh entries — explosive breakout
            candles still paint. Continuation and flip transitions are
            never affected by this flag.
    """

    candle_is_green = inputs.close > inputs.open
    candle_is_red = inputs.close < inputs.open
    is_fresh_entry = previous_display == CoreRegime.NEUTRAL

    # The corridor check applies only on fresh entries AND only when
    # the flag is True. Continuation paths always skip the check.
    apply_outer_check = is_fresh_entry and goldilocks_corridor_on_fresh_entry_only

    green_gate = (
        candle_is_green
        and inputs.close > inputs.upper_inner
        and (not apply_outer_check or inputs.close < inputs.upper_outer)
    )
    red_gate = (
        candle_is_red
        and inputs.close < inputs.lower_inner
        and (not apply_outer_check or inputs.close > inputs.lower_outer)
    )

    if current_core == CoreRegime.BULLISH:
        if green_gate:
            return CoreRegime.BULLISH
        return CoreRegime.BULLISH if previous_display == CoreRegime.BULLISH else CoreRegime.NEUTRAL

    if current_core == CoreRegime.BEARISH:
        if red_gate:
            return CoreRegime.BEARISH
        return CoreRegime.BEARISH if previous_display == CoreRegime.BEARISH else CoreRegime.NEUTRAL

    # Core is NEUTRAL — display follows directly
    return CoreRegime.NEUTRAL
