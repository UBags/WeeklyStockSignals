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

# ---- Full HH/HL/LH/LL structure + composite (spec v0.3 §6) ----
# The labels above only expose higher-valleys (HL) and lower-tops
# (LH). These add the missing higher-tops (HH) and lower-valleys
# (LL) and the composite trend quadrant. Convention: a "peak" is a
# sell/top turn (last_sell_value); a "trough" is a buy/valley turn
# (last_buy_value).
REL_NA = "NA"
REL_HH = "HH"   # higher high — latest peak above the prior peak
REL_LH = "LH"   # lower high  — latest peak below the prior peak
REL_HL = "HL"   # higher low  — latest trough above the prior trough
REL_LL = "LL"   # lower low   — latest trough below the prior trough

STRUCT_NA = "NA"
STRUCT_UPTREND = "UPTREND"          # HH + HL
STRUCT_DOWNTREND = "DOWNTREND"      # LH + LL
STRUCT_BROADENING = "BROADENING"    # HH + LL  (expanding / unstable)
STRUCT_CONTRACTING = "CONTRACTING"  # LH + HL  (coil / triangle)

STRUCTURE_RELATIONS = (REL_NA, REL_HH, REL_LH, REL_HL, REL_LL)
COMPOSITE_STRUCTURES = (
    STRUCT_NA, STRUCT_UPTREND, STRUCT_DOWNTREND,
    STRUCT_BROADENING, STRUCT_CONTRACTING,
)


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


def peak_relation(
    last_peak_value: float | None,
    prev_peak_value: float | None,
) -> str:
    """HH / LH / NA from the two most recent confirmed peaks (sell/top
    turns). HH if the latest peak is strictly above the prior one, LH
    if strictly below, NA if either is missing or they are equal."""
    if _is_nan(last_peak_value) or _is_nan(prev_peak_value):
        return REL_NA
    if last_peak_value > prev_peak_value:
        return REL_HH
    if last_peak_value < prev_peak_value:
        return REL_LH
    return REL_NA


def trough_relation(
    last_trough_value: float | None,
    prev_trough_value: float | None,
) -> str:
    """HL / LL / NA from the two most recent confirmed troughs (buy/
    valley turns). HL if the latest trough is strictly above the prior
    one, LL if strictly below, NA if either is missing or equal."""
    if _is_nan(last_trough_value) or _is_nan(prev_trough_value):
        return REL_NA
    if last_trough_value > prev_trough_value:
        return REL_HL
    if last_trough_value < prev_trough_value:
        return REL_LL
    return REL_NA


def composite_structure(peak_rel: str, trough_rel: str) -> str:
    """Collapse (peak_rel, trough_rel) into the trend-structure
    quadrant:

        HH + HL -> UPTREND
        LH + LL -> DOWNTREND
        HH + LL -> BROADENING     (expanding range / unstable)
        LH + HL -> CONTRACTING    (coil / triangle — precedes a move)

    Any NA on either side -> NA (structure not yet established)."""
    if peak_rel == REL_HH and trough_rel == REL_HL:
        return STRUCT_UPTREND
    if peak_rel == REL_LH and trough_rel == REL_LL:
        return STRUCT_DOWNTREND
    if peak_rel == REL_HH and trough_rel == REL_LL:
        return STRUCT_BROADENING
    if peak_rel == REL_LH and trough_rel == REL_HL:
        return STRUCT_CONTRACTING
    return STRUCT_NA


def structure_labels(
    *,
    last_peak_value: float | None,
    prev_peak_value: float | None,
    last_trough_value: float | None,
    prev_trough_value: float | None,
) -> tuple[str, str, str]:
    """(peak_rel, trough_rel, composite) — the full HH/HL/LH/LL
    structure the label layer only half-exposed. Needs no new state:
    the engine already tracks these four values (the same last /
    second-last buy and sell values fed to `pattern_label`). Map
    peak <- sell/top turn, trough <- buy/valley turn when calling."""
    p = peak_relation(last_peak_value, prev_peak_value)
    t = trough_relation(last_trough_value, prev_trough_value)
    return p, t, composite_structure(p, t)
