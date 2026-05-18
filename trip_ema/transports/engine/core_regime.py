"""Core regime tracker — port of Pine's `core_background` state machine.

This is a pure stepping function: given the previous core regime and the
current bar's inputs, return the new core regime. No mutable state lives
in this module; the caller (TickerEngine) holds the previous regime
between calls.

The state machine (from the Pine indicator):

    if core_background == BULLISH (1):
        # Stay bullish unless trend support has broken.
        # Support broken iff (price below long_ema AND long_ema not rising)
        #                OR (greenEMAFilter no longer holds).
        if support_broken:
            if priceBelowLower AND redEMAFilter:
                core = BEARISH   # direct flip
            else:
                core = NEUTRAL   # exit to neutral, wait for setup

    elif core_background == BEARISH (-1):
        # Mirror of bullish.

    else:  # NEUTRAL (0)
        if priceAboveUpper AND greenEMAFilter:
            core = BULLISH
        elif priceBelowLower AND redEMAFilter:
            core = BEARISH

The two EMA-alignment filters (`greenEMAFilter`, `redEMAFilter`) are also
ported here as pure helpers.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import CoreRegime


@dataclass(frozen=True, slots=True)
class CoreRegimeInputs:
    """Everything the core stepping function needs for one bar."""

    close: float
    source: float            # in the Pine indicator, source==close by default

    # Current and previous EMA values (for slope checks)
    med_ema: float
    med_ema_prev: float
    long_ema: float
    long_ema_prev: float
    longest_ema: float

    # Inner bands — gates for entry/flip
    upper_inner: float
    lower_inner: float


def green_ema_filter(inputs: CoreRegimeInputs) -> bool:
    """Pine's greenEMAFilter.

    Passes if EITHER of the faster EMAs is above the longest EMA,
    OR (both faster EMAs are rising AND price has cleared the longest EMA).
    The third clause is the "trend-momentum path" we added: it catches
    early reversals before the ribbon has fully unstacked.
    """
    static_align = (
        inputs.med_ema > inputs.longest_ema
        or inputs.long_ema > inputs.longest_ema
    )
    momentum_path = (
        inputs.med_ema > inputs.med_ema_prev
        and inputs.long_ema > inputs.long_ema_prev
        and inputs.source > inputs.longest_ema
    )
    return static_align or momentum_path


def red_ema_filter(inputs: CoreRegimeInputs) -> bool:
    """Mirror of green_ema_filter."""
    static_align = (
        inputs.med_ema < inputs.longest_ema
        or inputs.long_ema < inputs.longest_ema
    )
    momentum_path = (
        inputs.med_ema < inputs.med_ema_prev
        and inputs.long_ema < inputs.long_ema_prev
        and inputs.source < inputs.longest_ema
    )
    return static_align or momentum_path


def step_core_regime(
    previous: CoreRegime,
    inputs: CoreRegimeInputs,
) -> CoreRegime:
    """Compute the new core regime given the previous regime and current inputs.

    This is the heart of the indicator: a pure (previous, inputs) -> new
    function, trivially testable in isolation.
    """
    price_above_upper = inputs.close > inputs.upper_inner
    price_below_lower = inputs.close < inputs.lower_inner
    price_above_long_ema = inputs.close > inputs.long_ema
    price_below_long_ema = inputs.close < inputs.long_ema

    long_ema_slope_positive = inputs.long_ema > inputs.long_ema_prev
    long_ema_slope_negative = inputs.long_ema < inputs.long_ema_prev

    green = green_ema_filter(inputs)
    red = red_ema_filter(inputs)

    if previous == CoreRegime.BULLISH:
        support_broken = (price_below_long_ema and not long_ema_slope_positive) or not green
        if support_broken:
            return CoreRegime.BEARISH if (price_below_lower and red) else CoreRegime.NEUTRAL
        return CoreRegime.BULLISH

    if previous == CoreRegime.BEARISH:
        support_broken = (price_above_long_ema and not long_ema_slope_negative) or not red
        if support_broken:
            return CoreRegime.BULLISH if (price_above_upper and green) else CoreRegime.NEUTRAL
        return CoreRegime.BEARISH

    # Previous == NEUTRAL
    if price_above_upper and green:
        return CoreRegime.BULLISH
    if price_below_lower and red:
        return CoreRegime.BEARISH
    return CoreRegime.NEUTRAL
