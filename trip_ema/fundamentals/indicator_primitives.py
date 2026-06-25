"""Pine-Script-equivalent indicator primitives (Phase 5-2).

Pure functions, no DB access. Inputs are pandas Series indexed
by trade_date (or any monotonic index); outputs are also Series
of the same length with NA-leading entries where the indicator
isn't yet defined.

This module implements the math from the operator's Pine Script
"Weekly - Triple EMA with Background Strategy". The strategy
half (entry/exit, take-profit) is out of scope for Phase 5;
only the indicator computation lives here.

Three layers:

    Layer 1: pine_ema, pine_wma, pine_sma, pine_rma
             — direct Pine equivalents with matching NA semantics.
    Layer 2: hull_smooth, volume_weighted_ema, true_range, atr,
             hl_range_avg
             — composed indicators built on layer 1.
    Layer 3: triple_ema_stack
             — the three-EMA cascade with the operator's parameter
               sets.

Numerical correctness notes:
  - Pine's ta.ema seeds with src[0] on the first bar (NOT an SMA
    of the first n bars). We use pandas's .ewm(adjust=False), which
    matches this seeding.
  - Pine's ta.rma (Wilder's smoothing) uses alpha = 1/n, not
    2/(n+1). The ATR uses RMA, not EMA. Easy to get wrong.
  - Pine returns NA where an indicator isn't yet defined. We
    use pd.NA (via np.nan in float Series). Tests assert NaN
    presence explicitly so the wave-of-NaN behavior is locked.
  - Pine's math.floor truncates toward negative infinity. Python's
    int() truncates toward zero, which differs for negatives;
    we use math.floor explicitly.

Performance: pandas .ewm and .rolling are vectorized and fast
enough for daily-bar work (~250 bars/series/year × ~2300 NSE
tickers ≈ 580k bars). For minute-bar work this would need
optimization; not in Phase 5-2 scope.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from typing import cast

if TYPE_CHECKING:
    # The flat-shape IndicatorParams from the domain layer. Used by
    # `EngineParams.from_domain` to bridge in from the public/config
    # contract. Import is type-only to avoid a cycle:
    # domain.state has no fundamentals dependency, and we want to
    # keep it that way.
    from ..domain import IndicatorParams as DomainIndicatorParams


# ============================================================
# Layer 1: Pine Script primitive equivalents
# ============================================================


def pine_ema(src: pd.Series, n: int) -> pd.Series:
    """Pine Script ta.ema(src, n).

    EMA with alpha = 2/(n+1), seeded with src[0] on the first bar.
    For n=1, alpha=1 and the EMA equals the source.

    Why this matches Pine:
      - Pine's first-bar seed is the source value itself, not an
        SMA of the first n bars. This matches pandas's
        .ewm(adjust=False).mean() exactly.

    NA semantics: the first bar produces a value (the seed). If
    src has leading NAs, they propagate.

    Returns: a Series of the same length as src.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    # adjust=False uses recursive formula with src[0] as seed;
    # this matches Pine's ta.ema.
    return cast(pd.Series, src.ewm(span=n, adjust=False).mean())


def pine_wma(src: pd.Series, n: int) -> pd.Series:
    """Pine Script ta.wma(src, n).

    Linearly weighted moving average. The most recent bar gets
    weight n, the next gets n-1, ..., the oldest in the window
    gets 1. Sum of weights is n*(n+1)/2.

    NA semantics: returns NaN for the first (n-1) bars (the
    window isn't yet full).

    For n=1, this is just the source value.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if n == 1:
        return src.copy()
    # Weights: oldest..newest = 1, 2, ..., n
    weights = np.arange(1, n + 1, dtype=float)
    weight_sum = weights.sum()
    # rolling.apply lets us pass a custom weighted sum.
    return cast(pd.Series, src.rolling(window=n, min_periods=n).apply(
        lambda window: np.dot(window, weights) / weight_sum,
        raw=True,
    ))


def pine_sma(src: pd.Series, n: int) -> pd.Series:
    """Pine Script ta.sma(src, n).

    Simple moving average. Returns NaN for the first (n-1) bars."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    return cast(pd.Series, src.rolling(window=n, min_periods=n).mean())


def pine_rma(src: pd.Series, n: int) -> pd.Series:
    """Pine Script ta.rma(src, n) — Wilder's smoothing.

    EMA with alpha = 1/n (NOT 2/(n+1)). Used by ATR and RSI in
    Pine. Common bug: using ta.ema where ta.rma is needed —
    they're easy to confuse.

    Seeding matches ta.rma: first bar is src[0], subsequent bars
    apply alpha = 1/n. (Some references seed with SMA of first n;
    Pine's ta.rma uses single-bar seed, which is what we match.)
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    # alpha = 1/n means span = 2n - 1 (from alpha = 2/(span+1)).
    # Easier to specify alpha directly.
    return cast(pd.Series, src.ewm(alpha=1.0 / n, adjust=False).mean())


# ============================================================
# Layer 2: Composed indicators
# ============================================================


def hull_smooth(src: pd.Series, n: int) -> pd.Series:
    """Hull-style smoothing: ta.wma(2 * ta.wma(src, n/2) - ta.wma(src, n), sqrt(n)).

    The classical Hull Moving Average formula. Reduces lag while
    preserving smoothness. Used by the operator's Pine Script as
    the final-stage smoother on each EMA.

    Behavior at small n:
      - n = 1: floor(1/2) = 0, which is invalid for WMA. We
        treat n < 2 as "no smoothing" and return src directly.
      - n = 2: floor(2/2) = 1 (wma(x,1) = x), wma(x,2) proper,
        floor(sqrt(2)) = 1 (outer wma = identity). Net:
        2*x - wma(x, 2).
      - n = 4: floor(4/2) = 2, floor(sqrt(4)) = 2. Outer wma is
        a 2-period WMA.
      - n = 5: floor(5/2) = 2, floor(sqrt(5)) = 2.

    NA semantics: the longest inner window is `n`, so the first
    (n - 1) bars return NaN. The outer WMA adds floor(sqrt(n)) - 1
    more NaNs, but since the inner is already NaN at those bars,
    the net is (n - 1) + (sqrt_n - 1) - overlap. The math works
    out the same as testing empirically; we just document it as
    "leading NaNs until the indicator stabilizes".
    """
    if n < 2:
        return src.copy()
    half_n = math.floor(n / 2)
    sqrt_n = math.floor(math.sqrt(n))
    if half_n < 1:
        # Shouldn't happen given n >= 2 (then half_n >= 1) but
        # defensive.
        return src.copy()

    # Inner: 2 * wma(src, n/2) - wma(src, n)
    inner = 2 * pine_wma(src, half_n) - pine_wma(src, n)

    if sqrt_n < 2:
        # floor(sqrt(n)) = 1 means outer WMA collapses to identity.
        return inner
    return pine_wma(inner, sqrt_n)


def volume_weighted_ema(
    close: pd.Series,
    volume: pd.Series,
    n: int,
    *,
    volume_smoothing: int = 2,
) -> pd.Series:
    """Volume-weighted EMA, as defined in the operator's Pine Script.

    Formula:
        smoothed_vol = ta.sma(volume, volume_smoothing)
        vwema = ta.ema(close * smoothed_vol, n) / ta.ema(smoothed_vol, n)

    For constant volume (e.g. volume_smoothing=1, volume=constant),
    this degenerates to plain EMA of close. We use this property
    for industry indices where we synthesize volume=1.

    Edge case: if smoothed_vol has leading NaNs (it will, for
    the first volume_smoothing - 1 bars), the product close *
    smoothed_vol also has those leading NaNs, and pine_ema
    propagates them. The output's leading NaN count equals
    (volume_smoothing - 1).
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    smoothed_vol = pine_sma(volume, volume_smoothing)

    # Guard against division by zero. If smoothed_vol is zero or
    # missing for some bar, we'd get inf or NaN; replace post-hoc.
    numerator = pine_ema(close * smoothed_vol, n)
    denominator = pine_ema(smoothed_vol, n)

    # np.where doesn't preserve the pandas index; build the result
    # with proper alignment.
    result = numerator / denominator
    # When denominator is zero, the division yields inf. Replace
    # those with NaN so downstream code doesn't propagate inf
    # silently.
    result = result.replace([np.inf, -np.inf], np.nan)
    return result


def true_range(
    high: pd.Series, low: pd.Series, close: pd.Series,
    *, synthetic_for_index: bool = False,
) -> pd.Series:
    """True Range (TR) per bar.

    TR = max(high - low, |high - prev_close|, |low - prev_close|)

    The first bar's TR is simply (high - low) since prev_close
    is undefined. Pine handles this naturally with ta.na
    propagation; we replicate by using high - low when the
    other two terms produce NaN.

    synthetic_for_index: when True, returns |Δclose| × sqrt(2)
    instead. This is the operator's specification for industry
    indices, where high = low = close = value per bar and a
    real TR would degenerate to zero. The sqrt(2) factor is
    derived from the random-walk relationship between close-to-
    close σ and the expected intraday range.
    """
    if synthetic_for_index:
        # For an index series we get |Δclose| × sqrt(2). The first
        # bar has no prior close, so the diff is NaN — Pine and
        # we both leave that bar as NaN.
        return (close.diff().abs()) * math.sqrt(2)

    prev_close = close.shift(1)
    hl = high - low
    hc = (high - prev_close).abs()
    lc = (low - prev_close).abs()
    # On the first bar, prev_close is NaN → hc and lc are NaN
    # → max would be NaN. Substitute hl in that case.
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    # Re-fill the first bar (where hc/lc are NaN) with just hl.
    tr = tr.where(tr.notna(), hl)
    return tr


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series,
    n: int = 8, *, synthetic_for_index: bool = False,
) -> pd.Series:
    """Average True Range, Wilder-smoothed.

    ATR[i] = rma(TR, n)[i]

    For Pine parity, uses ta.rma (alpha = 1/n), NOT ta.ema. This
    is the most common bug when porting Pine ATR to other
    libraries — easy to use the wrong smoother.

    The synthetic_for_index flag flows through to true_range to
    enable the |Δclose|×sqrt(2) path for industry indices.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    tr = true_range(high, low, close, synthetic_for_index=synthetic_for_index)
    return pine_rma(tr, n)


def hl_range_avg(high: pd.Series, low: pd.Series, n: int = 26) -> pd.Series:
    """26-period SMA of (high - low). Used in the operator's table
    as the denominator of distance-from-EMA metrics.

    For industry indices where high = low, this is identically
    zero, which means distance-from-EMA metrics become undefined
    (divide by zero). Phase 5 code that computes those metrics
    must guard against this — see compute_indicator_state.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    return pine_sma(high - low, n)


# ============================================================
# Layer 3: Triple-EMA stack
# ============================================================


@dataclass(frozen=True, slots=True)
class TripleEMAParams:
    """Parameter set for one EMA in the Triple-EMA stack.

    Fields match the operator's Pine Script inputs exactly:

      base_len: ta.ema base length (the volume-weighted EMA
        period).
      first_smoothing: second ta.ema length applied to the VWEMA.
      hull_n: the `len2` parameter passed to hull_smooth (the
        Hull-style transform). This is what `math.floor(sqrt(n))`
        ultimately derives from.
      final_smoothing: optional outer ta.ema applied after Hull.
        The medium-term EMA in the Pine Script doesn't use this
        (set to None or 1); long and longest do.
    """
    base_len: int
    first_smoothing: int
    hull_n: int
    final_smoothing: int | None = None


# Default parameter sets, taken from the operator's updated Pine
# Script (the values in the input(...) calls).
MED_DEFAULT = TripleEMAParams(
    base_len=8, first_smoothing=4, hull_n=2, final_smoothing=None,
)
LONG_DEFAULT = TripleEMAParams(
    base_len=12, first_smoothing=8, hull_n=5, final_smoothing=4,
)
LONGEST_DEFAULT = TripleEMAParams(
    base_len=16, first_smoothing=9, hull_n=4, final_smoothing=4,
)


def single_ema(
    close: pd.Series,
    volume: pd.Series,
    params: TripleEMAParams,
    *,
    volume_smoothing: int = 2,
) -> pd.Series:
    """One leg of the Triple-EMA stack.

    Pipeline:
        1. vwema = volume_weighted_ema(close, vol, base_len)
        2. smoothed = ta.ema(vwema, first_smoothing)
        3. hull = hull_smooth(smoothed, hull_n)
        4. if params.final_smoothing: out = ta.ema(hull, final_smoothing)
           else: out = hull

    This matches the operator's Pine Script exactly. The medium-
    term EMA stops at stage 3; long and longest add stage 4.
    """
    vwema = volume_weighted_ema(
        close, volume, params.base_len,
        volume_smoothing=volume_smoothing,
    )
    smoothed = pine_ema(vwema, params.first_smoothing)
    hull = hull_smooth(smoothed, params.hull_n)
    if params.final_smoothing is not None and params.final_smoothing >= 2:
        return pine_ema(hull, params.final_smoothing)
    return hull


@dataclass(frozen=True, slots=True)
class TripleEMASeries:
    """Three EMA time series returned by triple_ema_stack."""
    med: pd.Series
    long: pd.Series
    longest: pd.Series


def triple_ema_stack(
    close: pd.Series,
    volume: pd.Series,
    *,
    med_params: TripleEMAParams = MED_DEFAULT,
    long_params: TripleEMAParams = LONG_DEFAULT,
    longest_params: TripleEMAParams = LONGEST_DEFAULT,
    volume_smoothing: int = 2,
) -> TripleEMASeries:
    """Compute all three EMAs at once.

    Returns a TripleEMASeries with three pandas Series, each the
    same length as `close`. Leading NaN counts depend on each
    EMA's parameter set.
    """
    return TripleEMASeries(
        med=single_ema(close, volume, med_params,
                       volume_smoothing=volume_smoothing),
        long=single_ema(close, volume, long_params,
                        volume_smoothing=volume_smoothing),
        longest=single_ema(close, volume, longest_params,
                           volume_smoothing=volume_smoothing),
    )


# ============================================================
# Layer 4: ATR bands
# ============================================================


@dataclass(frozen=True, slots=True)
class ATRBandParams:
    """Multipliers for the inner / mid / outer ATR bands around
    max(EMAs) and min(EMAs)."""
    inner_multiplier: float = 0.2
    mid_multiplier: float = 2.0
    outer_multiplier: float = 4.5


# Default from the operator's Pine Script.
ATR_BANDS_DEFAULT = ATRBandParams()


@dataclass(frozen=True, slots=True)
class ATRBands:
    """Per-bar band levels. Each is a pandas Series same length
    as the inputs."""
    upper_inner: pd.Series
    lower_inner: pd.Series
    upper_mid: pd.Series
    lower_mid: pd.Series
    upper_outer: pd.Series
    lower_outer: pd.Series


def compute_atr_bands(
    emas: TripleEMASeries,
    atr_series: pd.Series,
    *, params: ATRBandParams = ATR_BANDS_DEFAULT,
) -> ATRBands:
    """ATR bands as defined in the operator's Pine Script:
        upper_X = max(med, long, longest) + X_multiplier * ATR
        lower_X = min(med, long, longest) - X_multiplier * ATR

    Three sets: inner (0.2 × ATR, used for regime entry triggers),
    mid (2.0 × ATR, pierce markers), outer (4.5 × ATR, take-
    profit targets in the strategy half — not used by Phase 5
    but recorded for future phases).
    """
    # Stack the three EMA series side-by-side, take row-wise max/min.
    ema_df = pd.concat(
        [emas.med, emas.long, emas.longest], axis=1,
    )
    highest = ema_df.max(axis=1)
    lowest = ema_df.min(axis=1)

    return ATRBands(
        upper_inner=highest + params.inner_multiplier * atr_series,
        lower_inner=lowest - params.inner_multiplier * atr_series,
        upper_mid=highest + params.mid_multiplier * atr_series,
        lower_mid=lowest - params.mid_multiplier * atr_series,
        upper_outer=highest + params.outer_multiplier * atr_series,
        lower_outer=lowest - params.outer_multiplier * atr_series,
    )


# ============================================================
# Convenience: full per-bar computation
# ============================================================


@dataclass(frozen=True, slots=True)
class EngineParams:
    """All Phase 5 tunable parameters bundled together — the
    INTERNAL compute shape.

    This class groups per-EMA knobs into nested TripleEMAParams
    instances. Callers wiring up from config should not construct
    this directly; use `EngineParams.from_domain(...)` to bridge
    from the flat-shape `trip_ema.domain.IndicatorParams` instead.

    Defaults below match the operator's Pine Script and ARE
    DIFFERENT from the domain class's defaults. Don't reconcile
    them blindly — the divergence is intentional for industry
    indices (which use INDICATOR_DEFAULTS) vs. ticker series (which
    receive an explicit params built from config via from_domain).
    Operator should sign off on any change to the values below.
    """
    med: TripleEMAParams = MED_DEFAULT
    long: TripleEMAParams = LONG_DEFAULT
    longest: TripleEMAParams = LONGEST_DEFAULT
    atr_length: int = 8
    atr_bands: ATRBandParams = ATR_BANDS_DEFAULT
    hl_range_avg_length: int = 26
    volume_smoothing: int = 2

    @classmethod
    def from_domain(cls, p: DomainIndicatorParams) -> EngineParams:
        """Build an EngineParams from the flat-shape domain
        IndicatorParams that comes out of Config.to_indicator_params().

        The domain class is the public/config contract (one field per
        knob); EngineParams is the internal compute shape that groups
        per-EMA knobs into TripleEMAParams. This adapter is the only
        place that knows both shapes — every other caller should go
        through it rather than reaching into either type.

        Note on field names: the domain's flat names use the user-
        facing labels (`med_base`, `med_s1`, `med_s2`, `med_final`);
        TripleEMAParams uses the engine-internal labels
        (`base_len`, `first_smoothing`, `hull_n`, `final_smoothing`).
        The mapping is fixed by the Pine port and documented on
        TripleEMAParams.
        """
        return cls(
            med=TripleEMAParams(
                base_len=p.med_base, first_smoothing=p.med_s1,
                hull_n=p.med_s2, final_smoothing=p.med_final,
            ),
            long=TripleEMAParams(
                base_len=p.long_base, first_smoothing=p.long_s1,
                hull_n=p.long_s2, final_smoothing=p.long_final,
            ),
            longest=TripleEMAParams(
                base_len=p.longest_base, first_smoothing=p.longest_s1,
                hull_n=p.longest_s2, final_smoothing=p.longest_final,
            ),
            atr_length=p.atr_length,
            atr_bands=ATRBandParams(
                inner_multiplier=p.inner_mult,
                mid_multiplier=p.mid_mult,
                outer_multiplier=p.outer_mult,
            ),
            volume_smoothing=p.volume_sma_length,
        )

INDICATOR_DEFAULTS = EngineParams()


@dataclass(frozen=True, slots=True)
class IndicatorSeries:
    """All Phase 5-2 series outputs for a given input bar series.

    Phase 5-3 will add display_regime which are
    inherently sequential. Phase 5-2 stops at the indicators
    themselves.
    """
    emas: TripleEMASeries
    atr: pd.Series
    bands: ATRBands
    hl_range_avg: pd.Series
    true_range: pd.Series  # the underlying TR (pre-smoothing)


def compute_indicator_series(
    close: pd.Series,
    volume: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    params: EngineParams = INDICATOR_DEFAULTS,
    synthetic_atr_for_index: bool = False,
) -> IndicatorSeries:
    """Compute all per-bar indicator series for a given OHLCV input.

    For ticker bar series, pass real OHLCV; synthetic_atr_for_index
    stays False. For industry index series, pass high=low=close=value
    and volume=1, with synthetic_atr_for_index=True; the ATR path
    will compute |Δclose|×sqrt(2) per the operator's spec.

    Returns an IndicatorSeries with all 4 outputs aligned to the
    input index. The first N bars (where N depends on the longest
    smoothing chain) will have NaN values.
    """
    emas = triple_ema_stack(
        close, volume,
        med_params=params.med,
        long_params=params.long,
        longest_params=params.longest,
        volume_smoothing=params.volume_smoothing,
    )
    tr = true_range(
        high, low, close,
        synthetic_for_index=synthetic_atr_for_index,
    )
    atr_series = pine_rma(tr, params.atr_length)
    bands = compute_atr_bands(emas, atr_series, params=params.atr_bands)
    hlra = hl_range_avg(high, low, params.hl_range_avg_length)
    return IndicatorSeries(
        emas=emas,
        atr=atr_series,
        bands=bands,
        hl_range_avg=hlra,
        true_range=tr,
    )


# ============================================================
# Layer 5: Per-bar state assembly
# ============================================================
#
# Given a full input bar series + computed indicator Series, walk
# bar by bar and produce a list of IndicatorState dataclasses.
# This is where the sequential state (bars-since-turn, pattern
# labels) lives — the prior layers are pure functional.
#
# The regime states (display_regime) are NOT computed
# here — Phase 5-3 adds them in a separate walk because they have
# their own state machine and we want it isolated for clarity.
# For Phase 5-2, all produced IndicatorState rows have regime=0.


def _detect_turns(ema_series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Detect buy/sell turns on an EMA series.

    Pine:
        buy_turn  = (ema > ema[1]) and (ema[1] < ema[2])
        sell_turn = (ema < ema[1]) and (ema[1] > ema[2])

    Returns (buy_turns, sell_turns) — two boolean Series of the
    same length as the input. The first two bars are always False
    because the lookback condition isn't satisfied.

    Note: this detects the SECOND bar of an up-move (i.e. the
    confirmation bar). On bar i, we're saying "yesterday the EMA
    turned up because it had been falling before that."
    """
    prev = ema_series.shift(1)
    prev_prev = ema_series.shift(2)
    buy = (ema_series > prev) & (prev < prev_prev)
    sell = (ema_series < prev) & (prev > prev_prev)
    # Replace NaN with False (boolean Series fills NaN→False is
    # not automatic in pandas; we handle explicitly).
    buy = buy.fillna(False).astype(bool)
    sell = sell.fillna(False).astype(bool)
    return buy, sell


def _pattern_label_for_ema(
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
    """Return the pattern label for the current bar, given current
    turn signals and tracked state.

    Pine's logic (translated from the operator's script):
      mediumTermNote = if (mediumTerm2SuccessiveHigherValleys
                           or mediumTerm2SuccessiveHigherValleys[1]
                           or mediumTerm2SuccessiveHigherValleys[2]):
          "2 SUCCESSIVE HIGHER VALLEYS"
      else if (mediumTermHigherValley or ...[1] or ...[2]):
          "1 SUCCESSIVE HIGHER VALLEY"
      else if (mediumTerm2SuccessiveLowerTops or ...):
          "2 SUCCESSIVE LOWER TOPS"
      else if (mediumTermLowerTop or ...):
          "1 SUCCESSIVE LOWER TOP"
      else:
          "NA"

    The "or [1] or [2]" makes the label sticky for 3 bars after the
    detection bar. We replicate this by tracking the last detected
    label and emitting it for up to label_ttl_bars after detection.

    Detection conditions (Pine):
      2_higher_valleys: buy_turn AND source > last_buy AND last_buy >= second_last_buy
      1_higher_valley: buy_turn AND source > last_buy AND last_buy < second_last_buy
      2_lower_tops: sell_turn AND source < last_sell AND last_sell <= second_last_sell
      1_lower_top: sell_turn AND source < last_sell AND last_sell > second_last_sell

    Returns one of: 'NA', '1 SUCCESSIVE HIGHER VALLEY',
    '2 SUCCESSIVE HIGHER VALLEYS', '1 SUCCESSIVE LOWER TOP',
    '2 SUCCESSIVE LOWER TOPS'.
    """
    # First check: does this bar print a detection?
    if buy_turn_now and last_buy_value is not None:
        if source_now > last_buy_value:
            if (second_last_buy_value is not None
                and last_buy_value >= second_last_buy_value):
                return "2 SUCCESSIVE HIGHER VALLEYS"
            elif (second_last_buy_value is not None
                  and last_buy_value < second_last_buy_value):
                return "1 SUCCESSIVE HIGHER VALLEY"
    if sell_turn_now and last_sell_value is not None:
        if source_now < last_sell_value:
            if (second_last_sell_value is not None
                and last_sell_value <= second_last_sell_value):
                return "2 SUCCESSIVE LOWER TOPS"
            elif (second_last_sell_value is not None
                  and last_sell_value > second_last_sell_value):
                return "1 SUCCESSIVE LOWER TOP"

    # No fresh detection. Check stickiness: did one fire recently?
    # The Pine "or [1] or [2]" means within the last 3 bars
    # (current + 2 prior).
    if (bars_since_last_buy is not None
        and bars_since_last_buy < label_ttl_bars
        and last_buy_label != "NA"):
        return last_buy_label
    if (bars_since_last_sell is not None
        and bars_since_last_sell < label_ttl_bars
        and last_sell_label != "NA"):
        return last_sell_label
    return "NA"


@dataclass
class _PerEMATrackState:
    """Mutable per-EMA state carried bar to bar in the walk.

    Tracks the values at the last two buy turns and the last two
    sell turns (needed for the higher-valleys / lower-tops
    detection), plus bar-since-last-turn counters and the most
    recent emitted labels for stickiness.
    """
    last_buy_value: float | None = None
    second_last_buy_value: float | None = None
    last_sell_value: float | None = None
    second_last_sell_value: float | None = None
    last_buy_bar_idx: int | None = None
    last_sell_bar_idx: int | None = None
    last_buy_label: str = "NA"
    last_sell_label: str = "NA"


def compute_bar_states(
    close: pd.Series,
    volume: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    trade_dates: pd.Series,
    series: IndicatorSeries,
    params: EngineParams = INDICATOR_DEFAULTS,
    open_: pd.Series | None = None,
) -> list[dict]:
    """Walk the bar series and produce a per-bar dict of full
    indicator state values (including regime).

    Returns a list of dicts — one per bar in the input. Each dict
    has keys matching IndicatorState fields. The runner (Phase 5-4)
    will combine these with the series identity (ticker_id+timeframe
    or industry_name) to construct IndicatorState dataclasses
    for upsert.

    The function returns dicts rather than IndicatorState directly
    because:
      - IndicatorState has the polymorphic identity fields and
        we don't know which kind of series the caller has yet.
      - Dicts are easier for the runner to inspect/mutate before
        upsert.

    The `open_` parameter is needed by the display_regime gate
    (candle direction). For ticker bar series, pass the real open
    prices. For industry indices (synthetic OHLC where open==close),
    pass `close.shift(1)` to get day-over-day direction as the
    synthetic candle direction — otherwise candleIsGreen is always
    False for indices and display_regime can never enter +1.
    If open_ is None, it defaults to close.shift(1) (the index
    case).

    Bars where any required indicator is NaN (e.g. early bars
    before EMAs stabilize) are still emitted, but with NaN values
    in the relevant fields. Regime states stay at their current
    value (initially 0) when indicators are NaN; this is sticky
    behavior matching Pine semantics.

    Callers should filter for "indicator-ready" bars before
    persisting.
    """
    n = len(close)
    if n == 0:
        return []

    # Default open_ to prev_close for the index case.
    if open_ is None:
        open_ = cast(pd.Series, close.shift(1))

    # Compute the unified display_regime upfront (one walk over the bar
    # series). Step 9b replaced the old display_regime
    # split with a single regime — see `trip_ema/regime/compute_regime.py`
    # for the rules and `open_` is no longer consulted (the old
    # display-gate's candleIsGreen flag is gone in the unified model).
    regime_series = _compute_regime_series(close, series)

    # Detect turns on each EMA series.
    med_buys, med_sells = _detect_turns(series.emas.med)
    long_buys, long_sells = _detect_turns(series.emas.long)
    longest_buys, longest_sells = _detect_turns(series.emas.longest)

    # 52-period SMA of distance-from-EMA (in HL-range units).
    # We compute these once on the full series for vectorization.
    # On bars where hl_range_avg is 0 (index path) or NaN, the
    # distance is NaN.
    safe_hlra = series.hl_range_avg.where(series.hl_range_avg > 0)
    dist_to_med = (close - series.emas.med).abs() / safe_hlra
    dist_to_long = (close - series.emas.long).abs() / safe_hlra
    dist_to_longest = (close - series.emas.longest).abs() / safe_hlra
    mean_dist_to_med = pine_sma(dist_to_med, 52)
    mean_dist_to_long = pine_sma(dist_to_long, 52)
    mean_dist_to_longest = pine_sma(dist_to_longest, 52)

    # Pierce flags: high >= mid_band >= low (intra-bar wick).
    # Use bands from the computed series.
    pierces_upper_mid = (high >= series.bands.upper_mid) & (low <= series.bands.upper_mid)
    pierces_lower_mid = (high >= series.bands.lower_mid) & (low <= series.bands.lower_mid)
    pierces_upper_mid = pierces_upper_mid.fillna(False).astype(bool)
    pierces_lower_mid = pierces_lower_mid.fillna(False).astype(bool)

    # Per-EMA tracking state.
    med_state = _PerEMATrackState()
    long_state = _PerEMATrackState()
    longest_state = _PerEMATrackState()

    out: list[dict] = []
    for i in range(n):
        td = trade_dates.iloc[i]
        c = close.iloc[i]

        # Look up the values for this bar.
        med_v = series.emas.med.iloc[i]
        long_v = series.emas.long.iloc[i]
        longest_v = series.emas.longest.iloc[i]
        atr_v = series.atr.iloc[i]

        med_buy = bool(med_buys.iloc[i])
        med_sell = bool(med_sells.iloc[i])
        long_buy = bool(long_buys.iloc[i])
        long_sell = bool(long_sells.iloc[i])
        longest_buy = bool(longest_buys.iloc[i])
        longest_sell = bool(longest_sells.iloc[i])

        # Compute pattern labels BEFORE updating the per-EMA
        # tracking state — labels use the prior turns' values, not
        # the current bar's.
        bars_since_med_buy = (
            i - med_state.last_buy_bar_idx
            if med_state.last_buy_bar_idx is not None else None
        )
        bars_since_med_sell = (
            i - med_state.last_sell_bar_idx
            if med_state.last_sell_bar_idx is not None else None
        )
        bars_since_long_buy = (
            i - long_state.last_buy_bar_idx
            if long_state.last_buy_bar_idx is not None else None
        )
        bars_since_long_sell = (
            i - long_state.last_sell_bar_idx
            if long_state.last_sell_bar_idx is not None else None
        )
        bars_since_longest_buy = (
            i - longest_state.last_buy_bar_idx
            if longest_state.last_buy_bar_idx is not None else None
        )
        bars_since_longest_sell = (
            i - longest_state.last_sell_bar_idx
            if longest_state.last_sell_bar_idx is not None else None
        )

        med_label = _pattern_label_for_ema(
            med_buy, med_sell, c,
            med_state.last_buy_value, med_state.second_last_buy_value,
            med_state.last_sell_value, med_state.second_last_sell_value,
            med_state.last_buy_label, med_state.last_sell_label,
            bars_since_last_buy=bars_since_med_buy,
            bars_since_last_sell=bars_since_med_sell,
        )
        long_label = _pattern_label_for_ema(
            long_buy, long_sell, c,
            long_state.last_buy_value, long_state.second_last_buy_value,
            long_state.last_sell_value, long_state.second_last_sell_value,
            long_state.last_buy_label, long_state.last_sell_label,
            bars_since_last_buy=bars_since_long_buy,
            bars_since_last_sell=bars_since_long_sell,
        )
        longest_label = _pattern_label_for_ema(
            longest_buy, longest_sell, c,
            longest_state.last_buy_value, longest_state.second_last_buy_value,
            longest_state.last_sell_value, longest_state.second_last_sell_value,
            longest_state.last_buy_label, longest_state.last_sell_label,
            bars_since_last_buy=bars_since_longest_buy,
            bars_since_last_sell=bars_since_longest_sell,
        )

        # Compute "bars since last turn" + "% change since last turn"
        # for the table view. The choice between buy or sell direction
        # follows the EMA's CURRENT slope: if med_v > med_v[i-1], we
        # report bars-since-buy and % change since buy.
        bars_since_med_turn, pct_change_med = _bars_and_pct_since_turn(
            i, close, med_state, med_v,
            prev_ema=series.emas.med.iloc[i - 1] if i > 0 else None,
        )
        bars_since_long_turn, pct_change_long = _bars_and_pct_since_turn(
            i, close, long_state, long_v,
            prev_ema=series.emas.long.iloc[i - 1] if i > 0 else None,
        )
        bars_since_longest_turn, pct_change_longest = _bars_and_pct_since_turn(
            i, close, longest_state, longest_v,
            prev_ema=series.emas.longest.iloc[i - 1] if i > 0 else None,
        )

        # Now update the per-EMA tracking state for this bar.
        for state, buy, sell in [
            (med_state, med_buy, med_sell),
            (long_state, long_buy, long_sell),
            (longest_state, longest_buy, longest_sell),
        ]:
            if buy:
                state.second_last_buy_value = state.last_buy_value
                state.last_buy_value = c
                state.last_buy_bar_idx = i
            if sell:
                state.second_last_sell_value = state.last_sell_value
                state.last_sell_value = c
                state.last_sell_bar_idx = i
        # Update sticky labels.
        if med_label != "NA":
            if "VALLEY" in med_label:
                med_state.last_buy_label = med_label
            else:
                med_state.last_sell_label = med_label
        if long_label != "NA":
            if "VALLEY" in long_label:
                long_state.last_buy_label = long_label
            else:
                long_state.last_sell_label = long_label
        if longest_label != "NA":
            if "VALLEY" in longest_label:
                longest_state.last_buy_label = longest_label
            else:
                longest_state.last_sell_label = longest_label

        out.append({
            "trade_date": td,
            "close": c,
            "med_ema": med_v,
            "long_ema": long_v,
            "longest_ema": longest_v,
            "atr": atr_v,
            "upper_inner_band": series.bands.upper_inner.iloc[i],
            "lower_inner_band": series.bands.lower_inner.iloc[i],
            "upper_mid_band": series.bands.upper_mid.iloc[i],
            "lower_mid_band": series.bands.lower_mid.iloc[i],
            "upper_outer_band": series.bands.upper_outer.iloc[i],
            "lower_outer_band": series.bands.lower_outer.iloc[i],
            "display_regime": int(regime_series.iloc[i]),
            "pierces_upper_mid": bool(pierces_upper_mid.iloc[i]),
            "pierces_lower_mid": bool(pierces_lower_mid.iloc[i]),
            "dist_to_med_in_hl_units": _none_if_nan(dist_to_med.iloc[i]),
            "dist_to_long_in_hl_units": _none_if_nan(dist_to_long.iloc[i]),
            "dist_to_longest_in_hl_units": _none_if_nan(dist_to_longest.iloc[i]),
            "mean_dist_to_med_in_hl_units": _none_if_nan(mean_dist_to_med.iloc[i]),
            "mean_dist_to_long_in_hl_units": _none_if_nan(mean_dist_to_long.iloc[i]),
            "mean_dist_to_longest_in_hl_units": _none_if_nan(mean_dist_to_longest.iloc[i]),
            "bars_since_med_turn": bars_since_med_turn,
            "bars_since_long_turn": bars_since_long_turn,
            "bars_since_longest_turn": bars_since_longest_turn,
            "pct_change_since_med_turn": pct_change_med,
            "pct_change_since_long_turn": pct_change_long,
            "pct_change_since_longest_turn": pct_change_longest,
            "med_pattern_label": med_label,
            "long_pattern_label": long_label,
            "longest_pattern_label": longest_label,
            "med_turn_buy": med_buy,
            "med_turn_sell": med_sell,
            "long_turn_buy": long_buy,
            "long_turn_sell": long_sell,
            "longest_turn_buy": longest_buy,
            "longest_turn_sell": longest_sell,
        })

    return out


def _bars_and_pct_since_turn(
    i: int,
    close: pd.Series,
    state: _PerEMATrackState,
    ema_now: float,
    prev_ema: float | None,
) -> tuple[int | None, float | None]:
    """Compute (bars_since_turn, pct_change_since_turn) for one EMA
    at bar i, following the Pine table's display logic.

    Display direction follows the EMA's current slope:
      - If ema_now > prev_ema (EMA rising), use last_buy_bar_idx.
        pct_change = (close[i] - close[turn_bar]) / close[turn_bar] × 100
      - If ema_now < prev_ema (EMA falling), use last_sell_bar_idx.
        pct_change = -(close[i] - close[turn_bar]) / close[turn_bar] × 100
        (negated, because we're measuring the down-move from a sell)
      - If ema is flat or NaN, return (None, None).
    """
    if prev_ema is None or pd.isna(prev_ema) or pd.isna(ema_now):
        return None, None
    if ema_now > prev_ema:
        if state.last_buy_bar_idx is None:
            return None, None
        n_bars = i - state.last_buy_bar_idx
        turn_close = close.iloc[state.last_buy_bar_idx]
        if turn_close == 0:
            return n_bars, None
        pct = (close.iloc[i] - turn_close) / turn_close * 100.0
        return n_bars, float(pct)
    elif ema_now < prev_ema:
        if state.last_sell_bar_idx is None:
            return None, None
        n_bars = i - state.last_sell_bar_idx
        turn_close = close.iloc[state.last_sell_bar_idx]
        if turn_close == 0:
            return n_bars, None
        pct = -(close.iloc[i] - turn_close) / turn_close * 100.0
        return n_bars, float(pct)
    else:
        # Flat EMA — no direction to report.
        return None, None


def _none_if_nan(value):
    """Convert NaN to Python None (for IndicatorState's Optional[float])."""
    if value is None:
        return None
    if pd.isna(value):
        return None
    return float(value)


# ============================================================
# Layer 6: Unified regime (Step 9b)
# ============================================================
#
# Replaces the OLD display_regime state-machine pair
# with a single regime computed per bar by the new
# `trip_ema.regime.compute_regime` function. One walk through the
# bar series; one regime value per bar; no candle-direction gate.
#
# The dict key produced by `compute_indicator_state_for_bars` is
# still `display_regime` (preserving the schema column name); its
# value now comes from this walker.
#
# Spec for the new regime rules — see
# `trip_ema/regime/compute_regime.py` for the canonical source:
#
#   (a) definite_green     close >= upper_inner
#   (b) definite_red       close <= lower_inner
#   (c) green_continuation prev_green AND (close >= lower_env OR
#                          (close >= lower_inner AND
#                           prev_close >= prev_lower_env))
#   (d) red_continuation   mirror of (c)
#   (e) neutral            otherwise
#
# Envelope = max / min of the three EMAs (med, long, longest).


def _compute_regime_series(close: pd.Series, series) -> pd.Series:
    """Walk the bar series and apply `regime.compute_regime` per bar.

    Returns a Series of int64 values in {-1, 0, +1}, same length as
    `close`. On bars where any required indicator value is NaN
    (warmup), the regime is sticky — we hold the previous regime
    rather than resetting to NEUTRAL, matching the OLD
    `compute_core_regime` behaviour and matching what live state
    threading would do.
    """
    from ..regime import BarInputs, Regime, compute_regime

    n = len(close)
    if n == 0:
        return pd.Series([], dtype="int64")

    out = np.zeros(n, dtype=np.int64)
    prev_regime: Regime = Regime.NEUTRAL
    prev_close: float | None = None
    prev_lower_env: float | None = None
    prev_upper_env: float | None = None

    for i in range(n):
        c = close.iloc[i]
        med = series.emas.med.iloc[i]
        long_ = series.emas.long.iloc[i]
        longest = series.emas.longest.iloc[i]
        ui = series.bands.upper_inner.iloc[i]
        li = series.bands.lower_inner.iloc[i]
        uo = series.bands.upper_outer.iloc[i]
        lo = series.bands.lower_outer.iloc[i]

        # Sticky-on-NaN: warmup bars carry the prior regime forward
        # rather than dropping to NEUTRAL. Matches the OLD
        # compute_core_regime semantics.
        if (pd.isna(c) or pd.isna(med) or pd.isna(long_)
                or pd.isna(longest)
                or pd.isna(ui) or pd.isna(li)
                or pd.isna(uo) or pd.isna(lo)):
            out[i] = int(prev_regime)
            continue

        upper_env = float(max(med, long_, longest))
        lower_env = float(min(med, long_, longest))
        bar_inputs = BarInputs(
            close=float(c),
            upper_inner=float(ui), lower_inner=float(li),
            upper_outer=float(uo), lower_outer=float(lo),
            upper_envelope=upper_env, lower_envelope=lower_env,
        )
        current = compute_regime(
            previous_regime=prev_regime,
            previous_close=prev_close,
            previous_lower_envelope=prev_lower_env,
            previous_upper_envelope=prev_upper_env,
            bar=bar_inputs,
        )
        out[i] = int(current)
        prev_regime = current
        prev_close = float(c)
        prev_lower_env = lower_env
        prev_upper_env = upper_env

    return pd.Series(out, index=close.index)