"""Indicator-state types: regimes, signals, per-bar state, params, timeframe.

All immutable. No I/O. No imports from any other trip_ema module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum


class Timeframe(str, Enum):
    """Bar interval. String value matches what's stored in the DB
    `ohlcv_bars.timeframe` column."""

    MIN_15 = "15m"
    HOURLY = "1h"
    HOURLY_2 = "2h"
    DAILY = "1D"
    WEEKLY = "1W"

    @property
    def total_seconds(self) -> int:
        """Bucket duration in seconds. Used by aggregation logic to
        figure out 'is now still inside this bucket?'."""
        return {
            "15m": 15 * 60,
            "1h": 60 * 60,
            "2h": 2 * 60 * 60,
            "1D": 24 * 60 * 60,
            "1W": 7 * 24 * 60 * 60,
        }[self.value]


class CoreRegime(IntEnum):
    """Three-state regime label. Used both for `core_background` (the
    underlying regime tracker) and `display_background` (the gated render
    state). Same type, two meanings, intentionally."""

    BEARISH = -1
    NEUTRAL = 0
    BULLISH = 1


class ChangeSignal(IntEnum):
    """Classification of a regime transition.

    NO_CHANGE: display_bg unchanged.
    BUY:  display_bg became BULLISH (from any other state).
    SELL: display_bg became BEARISH (from any other state).
    EXIT_FROM_BUY:  display_bg went BULLISH -> NEUTRAL.
    EXIT_FROM_SELL: display_bg went BEARISH -> NEUTRAL.
    """

    NO_CHANGE = 0
    BUY = 1
    SELL = -1
    EXIT_FROM_BUY = 2
    EXIT_FROM_SELL = -2


@dataclass(frozen=True, slots=True)
class IndicatorParams:
    """All tunable parameters for the indicator computation.

    Loaded once from config at startup; the same instance is used for every
    ticker. If per-ticker tuning is ever required, this becomes a per-call
    argument instead of a singleton.

    EMA construction note: each of med/long/longest goes through a 3-stage
    process — volume-weighted EMA (base length), then EMA smoothing (s1),
    then Hull-style WMA polish (s2). The `final` parameter is an optional
    fourth EMA stage applied after the WMA; Pine's `long` and `longest` use
    it, `med` does not.
    """

    # Medium EMA
    med_base: int = 8
    med_s1: int = 4
    med_s2: int = 4
    med_final: int | None = None

    # Long EMA
    long_base: int = 15
    long_s1: int = 4
    long_s2: int = 4
    long_final: int | None = 3

    # Longest EMA
    longest_base: int = 28
    longest_s1: int = 7
    longest_s2: int = 5
    longest_final: int | None = 3

    # ATR and bands
    atr_length: int = 14
    inner_mult: float = 0.2
    mid_mult: float = 2.0
    outer_mult: float = 4.0

    # Volume smoothing — the Pine `Weekly_Triple_EMA.pine` script wraps
    # the raw `volume` series in `ta.sma(volume, 2)` before feeding it
    # into the VW-EMA's numerator and denominator. Configurable here so
    # the same Python engine can run other indicators with different
    # smoothing without code change.
    #   1 = no smoothing (use raw bar volume — what the engine did before B1)
    #   2 = match Pine's Weekly_Triple_EMA
    volume_sma_length: int = 2

    # Behavior flags
    goldilocks_corridor_on_fresh_entry_only: bool = True
    """When True (faithful Pine port), a fresh entry from NEUTRAL requires
    the close to be inside the Goldilocks corridor (past inner band, before
    outer band). When False, the outer-band check is dropped — explosive
    breakout candles still paint.

    Continuation and flip-from-opposite-color paths are never gated by
    the outer band; only fresh entries are. So this flag affects only
    the NEUTRAL → BULLISH and NEUTRAL → BEARISH transitions.
    """


@dataclass(frozen=True, slots=True)
class BarState:
    """The complete derived state for a single bar.

    Returned from `TickerEngine.ingest(bar)`. Stored in `state_history`.
    Serialized to the SignalCache and to `ticker_signal_state.state_json`.
    """

    timestamp: datetime

    # EMAs
    med_ema: float
    long_ema: float
    longest_ema: float

    # ATR
    atr: float

    # Inner bands (used by display gate)
    upper_inner: float
    lower_inner: float

    # Mid bands (used only for pierce markers, not regime logic)
    upper_mid: float
    lower_mid: float

    # Outer bands (used by display gate for fresh-entry corridor)
    upper_outer: float
    lower_outer: float

    # Pierce flags — did this bar's range touch the mid bands?
    pierces_upper_mid: bool
    pierces_lower_mid: bool

    # Regime — core is the tracker, display is what would be painted
    core_bg: CoreRegime
    display_bg: CoreRegime

    @property
    def is_warmed_up(self) -> bool:
        """True iff every indicator value on this state is finite (no NaN).

        Pine's `ta.ema` / `ta.atr` / `ta.sma` return NaN during their
        respective warmup phases (see Architecture invariant 3.3). The
        Python port matches that exactly. Downstream consumers — the
        regime classifier, the change detector, dashboards — should
        treat a NOT-warmed-up state as "no signal yet" rather than
        attempting to derive meaning from NaN-laced bands.

        We check the EMAs and ATR because they're the upstream sources;
        if any one is NaN the bands derived from them will be too. We
        intentionally do NOT inspect `core_bg` / `display_bg` here —
        the regime is an enum (always valid); the question is whether
        the *numbers* it was computed from are real.
        """
        return (
            not math.isnan(self.med_ema)
            and not math.isnan(self.long_ema)
            and not math.isnan(self.longest_ema)
            and not math.isnan(self.atr)
        )
