"""ATR-derived bands and pierce detection.

These are pure stateless calculations from EMA values and ATR. No carrying
state is required — they're just arithmetic on the current bar's values.

The bands are constructed relative to the *highest* and *lowest* of the
three EMAs (so they hug the ribbon's outer extents, not its center).
"""

from __future__ import annotations

from typing import NamedTuple

from ..domain import IndicatorParams


class Bands(NamedTuple):
    """All six ATR-derived levels for one bar."""

    upper_inner: float
    lower_inner: float
    upper_mid: float
    lower_mid: float
    upper_outer: float
    lower_outer: float


def compute_bands(
    med_ema: float,
    long_ema: float,
    longest_ema: float,
    atr: float,
    params: IndicatorParams,
) -> Bands:
    """Compute the six band values given the three EMA values and ATR.

    Caller is responsible for passing non-NaN inputs. If any input is NaN,
    all outputs will be NaN (NaN arithmetic propagates).
    """
    highest = max(med_ema, long_ema, longest_ema)
    lowest = min(med_ema, long_ema, longest_ema)
    return Bands(
        upper_inner=highest + params.inner_mult * atr,
        lower_inner=lowest - params.inner_mult * atr,
        upper_mid=highest + params.mid_mult * atr,
        lower_mid=lowest - params.mid_mult * atr,
        upper_outer=highest + params.outer_mult * atr,
        lower_outer=lowest - params.outer_mult * atr,
    )


def detects_upper_mid_pierce(high: float, low: float, upper_mid: float) -> bool:
    """A bar pierces the upper mid level iff its range straddles the level.

    Equivalently: high >= level AND low <= level. This is the geometric
    "the horizontal line touches the candle" definition.
    """
    return high >= upper_mid and low <= upper_mid


def detects_lower_mid_pierce(high: float, low: float, lower_mid: float) -> bool:
    """A bar pierces the lower mid level iff its range straddles the level."""
    return high >= lower_mid and low <= lower_mid
