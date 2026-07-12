"""Pure regime classifier — the single canonical implementation.

Given (previous_regime, prev_close, prev_lower_envelope, prev_upper_envelope,
current_bar_inputs), returns the current bar's `Regime`.

No I/O. No state. No imports outside `domain/`. This function is the
only file in the codebase that implements the regime rules — every
other consumer either calls this directly or reads the persisted
`stock_indicator_state.display_regime` (and the industry table's)
column populated from this call.

Threading: stateless and idempotent. Safe to call concurrently.

Numeric edge cases:
  - Boundary equality (e.g. close == upper_inner) hits the rule it
    falls within, per the spec's `>=` / `<=` inequalities. A bar
    sitting exactly on the upper inner band is definite_green.
  - NaN inputs (warmup) propagate to `False` on every comparison and
    fall through to NEUTRAL. The caller is responsible for not
    over-reading meaning into NEUTRAL-during-warmup vs
    NEUTRAL-as-the-rule-said-so.
  - prev_close / prev_lower_envelope / prev_upper_envelope are `None`
    on the very first bar of a (ticker, timeframe) series. The
    continuation rules cannot fire when these are None — guarded
    explicitly below rather than relying on NaN semantics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from trip_ema.domain import CoreRegime as Regime  # SMALLINT encoding: -1, 0, +1


@dataclass(frozen=True, slots=True)
class BarInputs:
    """All inputs to `compute_regime` for the current bar.

    The four band values and the two envelope values are the same
    quantities the existing `indicators/bands.py` already computes;
    callers pass them in rather than recomputing here so the regime
    package stays free of indicator-math dependencies.
    """

    close: float
    upper_inner: float
    lower_inner: float
    upper_outer: float
    lower_outer: float
    upper_envelope: float
    """Max of (med_ema, long_ema, longest_ema) for this bar."""
    lower_envelope: float
    """Min of (med_ema, long_ema, longest_ema) for this bar."""


def _is_finite(*values: float | None) -> bool:
    """True iff every value is a real finite float (rules out NaN, inf, None)."""
    for v in values:
        if v is None:
            return False
        if not math.isfinite(v):
            return False
    return True


def compute_regime(
    previous_regime: Regime,
    previous_close: float | None,
    previous_lower_envelope: float | None,
    previous_upper_envelope: float | None,
    bar: BarInputs,
) -> Regime:
    """Classify the current bar's regime.

    Returns one of `Regime.BULLISH`, `Regime.BEARISH`, `Regime.NEUTRAL`.

    Rules are evaluated top-to-bottom; the first match wins:

      (a) definite_green     — close >= upper_inner
      (b) definite_red       — close <= lower_inner
      (c) green_continuation — prev was green AND price still inside
                               the green corridor
      (d) red_continuation   — prev was red AND price still inside
                               the red corridor
      (e) neutral            — otherwise

    A bar that satisfies BOTH (a) and (d) is GREEN (priority). A bar
    that satisfies BOTH (b) and (c) is RED (priority). This is what
    makes a sharp reversal paint immediately rather than continuing
    the prior color.

    Warmup: if any of the current bar's bands or envelope values are
    NaN, the rule comparisons return False and we fall through to
    NEUTRAL. No special-case branch needed — NaN semantics carry it.
    """
    # Warmup guard — explicit, not relying on NaN comparison semantics,
    # because Python's behaviour around NaN comparisons is subtle and
    # we want the warmup case to be unmistakably explicit.
    if not _is_finite(
        bar.close,
        bar.upper_inner, bar.lower_inner,
        bar.upper_envelope, bar.lower_envelope,
    ):
        return Regime.NEUTRAL

    # (a) definite_green — close >= upper_inner ATR boundary
    if bar.close >= bar.upper_inner:
        return Regime.BULLISH

    # (b) definite_red — close <= lower_inner ATR boundary
    if bar.close <= bar.lower_inner:
        return Regime.BEARISH

    # (c) green_continuation — only if the previous bar was green AND we
    # have the previous-bar inputs the second leg needs.
    if previous_regime == Regime.BULLISH:
        # First leg: close still above the lower envelope (ribbon bottom).
        leg1 = bar.close >= bar.lower_envelope
        # Second leg: today closed above the lower-inner band AND yesterday's
        # close was above yesterday's lower envelope. Captures the "ribbon
        # dipped but price held the inner band" case. Requires both prev
        # values to be finite.
        leg2 = (
            _is_finite(previous_close, previous_lower_envelope)
            and bar.close >= bar.lower_inner
            and previous_close >= previous_lower_envelope  # type: ignore[operator]
        )
        if leg1 or leg2:
            return Regime.BULLISH

    # (d) red_continuation — mirror of (c).
    if previous_regime == Regime.BEARISH:
        leg1 = bar.close <= bar.upper_envelope
        leg2 = (
            _is_finite(previous_close, previous_upper_envelope)
            and bar.close <= bar.upper_inner
            and previous_close <= previous_upper_envelope  # type: ignore[operator]
        )
        if leg1 or leg2:
            return Regime.BEARISH

    # (e) neutral — nothing else matched.
    return Regime.NEUTRAL
