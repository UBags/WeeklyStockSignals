"""Shared indicator business rules — pure, no pandas, no I/O.

Extracted so that BOTH the (archived) pandas walker and the new
state-carry engine import the *same* rule logic. This is the anti-
duplication seam: the numeric series layer differs between the two
implementations, but the business rules (pattern labels, bars/pct
since turn) live here once and are called identically by both.

`compute_regime` is already shared via `trip_ema.regime`; it is not
duplicated here.

Nothing in this module touches pandas or numpy — every function takes
and returns plain Python scalars, so the stateful engine (which has no
Series) and the pandas walker (which passes `series.iloc[...]` values)
both call in the same way.
"""
from __future__ import annotations

import math

# Pattern-label vocabulary (unchanged from the Pine port).
LABEL_NA = "NA"
LABEL_2_HIGHER_VALLEYS = "2 SUCCESSIVE HIGHER VALLEYS"
LABEL_1_HIGHER_VALLEY = "1 SUCCESSIVE HIGHER VALLEY"
LABEL_2_LOWER_TOPS = "2 SUCCESSIVE LOWER TOPS"
LABEL_1_LOWER_TOP = "1 SUCCESSIVE LOWER TOP"


def _is_nan(x: float | None) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def pattern_label(
    buy_turn_now: bool,
    sell_turn_now: bool,
    source_now: float,
    last_buy_value: float | None,
    second_last_buy_value: float | None,
    last_sell_value: float | None,
    second_last_sell_value: float | None,
    last_buy_label: str,
    last_sell_label: str,
    *,
    label_ttl_bars: int = 3,
    bars_since_last_buy: int | None = None,
    bars_since_last_sell: int | None = None,
) -> str:
    """Pattern label for the current bar, from turn signals + tracked
    state. Pure port of the Pine higher-valleys / lower-tops logic.

    Detection (Pine):
      2_higher_valleys: buy_turn AND source > last_buy AND
                        last_buy >= second_last_buy
      1_higher_valley:  buy_turn AND source > last_buy AND
                        last_buy <  second_last_buy
      2_lower_tops:     sell_turn AND source < last_sell AND
                        last_sell <= second_last_sell
      1_lower_top:      sell_turn AND source < last_sell AND
                        last_sell >  second_last_sell

    The Pine `or [1] or [2]` makes a fired label sticky for
    `label_ttl_bars` bars; we replicate via the last-label + bars-since
    trackers.
    """
    # Fresh detection this bar?
    if buy_turn_now and last_buy_value is not None:
        if source_now > last_buy_value:
            if (second_last_buy_value is not None
                    and last_buy_value >= second_last_buy_value):
                return LABEL_2_HIGHER_VALLEYS
            elif (second_last_buy_value is not None
                  and last_buy_value < second_last_buy_value):
                return LABEL_1_HIGHER_VALLEY
    if sell_turn_now and last_sell_value is not None:
        if source_now < last_sell_value:
            if (second_last_sell_value is not None
                    and last_sell_value <= second_last_sell_value):
                return LABEL_2_LOWER_TOPS
            elif (second_last_sell_value is not None
                  and last_sell_value > second_last_sell_value):
                return LABEL_1_LOWER_TOP

    # No fresh detection — stickiness within the last label_ttl_bars.
    if (bars_since_last_buy is not None
            and bars_since_last_buy < label_ttl_bars
            and last_buy_label != LABEL_NA):
        return last_buy_label
    if (bars_since_last_sell is not None
            and bars_since_last_sell < label_ttl_bars
            and last_sell_label != LABEL_NA):
        return last_sell_label
    return LABEL_NA


def bars_and_pct_since_turn(
    *,
    ema_now: float | None,
    prev_ema: float | None,
    current_idx: int,
    current_close: float,
    last_buy_bar_idx: int | None,
    last_buy_close: float | None,
    last_sell_bar_idx: int | None,
    last_sell_close: float | None,
) -> tuple[int | None, float | None]:
    """(bars_since_turn, pct_change_since_turn) for one EMA, by the
    Pine display rule. Pure scalar core — callers pass the close at the
    turn bar directly (the pandas walker reads it via `.iloc`; the
    engine tracks it as state).

    Direction follows EMA slope:
      rising  (ema_now > prev_ema) -> measure from the last BUY turn
      falling (ema_now < prev_ema) -> measure from the last SELL turn,
                                       pct negated (down-move magnitude)
      flat / NaN                   -> (None, None)
    """
    if prev_ema is None or _is_nan(prev_ema) or _is_nan(ema_now):
        return None, None
    if ema_now > prev_ema:
        if last_buy_bar_idx is None or last_buy_close is None:
            return None, None
        n_bars = current_idx - last_buy_bar_idx
        if last_buy_close == 0:
            return n_bars, None
        pct = (current_close - last_buy_close) / last_buy_close * 100.0
        return n_bars, float(pct)
    elif ema_now < prev_ema:
        if last_sell_bar_idx is None or last_sell_close is None:
            return None, None
        n_bars = current_idx - last_sell_bar_idx
        if last_sell_close == 0:
            return n_bars, None
        pct = -(current_close - last_sell_close) / last_sell_close * 100.0
        return n_bars, float(pct)
    else:
        return None, None
