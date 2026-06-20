"""Stateless compute — ad-hoc/debug entry point.

# Step 8 hollow

This module previously built a throwaway `TickerEngine`, primed it
with supplied history, ingested a latest bar, and returned a verbose
or terse response. Under Step 9 demolition the engine is deleted —
so this file is rewritten to use the new pure rules
(`regime.compute_regime`, `signals.generate_signal`) directly. The
`POST /v1/compute` route keeps working through the transition.

The function shape is preserved: same args, same return types. Only
the implementation changes.

Caller expectations vs reality:
  - Caller provides WEEKLY bars already (it's their responsibility
    to aggregate from daily if needed; the route doesn't do daily
    inputs). We walk them as-is.
  - Caller provides `history` plus a `latest_bar`. We walk
    `history + [latest_bar]` as one continuous sequence.
  - `latest_change` is derived from the regime transition between
    the second-to-last and last bars, mapped to the corresponding
    ChangeSignal. This matches the OLD module's behaviour: it
    classified by regime delta, not by the new spec's state
    machine. The state machine needs prior position-state which
    a stateless call doesn't have, so we stick with the regime
    delta — it's the right semantic for a debug endpoint.

This is intentionally a thin shim. It exists so the existing
`/v1/compute` route doesn't 410 after Step 9; if you need
production-grade signal emission with proper state threading, use
the live path (Step 6) or the backfill walker (Step 5).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from ..domain import (
    BarState,
    ChangeSignal,
    CoreRegime,
    IndicatorParams,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from ..fundamentals.indicator_primitives import compute_indicator_series
from ..regime import BarInputs, Regime, compute_regime
from .responses import TerseResponse, VerboseResponse


# ============================================================
# Public entry points (signatures preserved from the legacy)
# ============================================================


def compute_full_verbose(
    *,
    ticker: TickerRef,
    history: list[OHLCVBar],
    latest_bar: OHLCVBar,
    timeframe: Timeframe,
    params: IndicatorParams,
    state_history_retain: int = 200,
) -> VerboseResponse:
    """Walk `history + [latest_bar]`, compute regime per bar, return
    the per-bar BarStates as a VerboseResponse.

    Args: same as the legacy. `state_history_retain` clips the
    returned `bars` tuple to at most N most-recent entries.

    The walk uses `compute_regime` (Step 1) for the regime
    trajectory; `latest_change` is the regime transition between the
    last two bars, mapped to ChangeSignal. No DB, no cache, no
    state-machine threading.
    """
    bars = list(history) + [latest_bar]
    states, _trajectory = _walk_bars(bars, params=params, timeframe=timeframe)
    if not states:
        # Caller passed an empty history AND latest_bar produced no
        # state (shouldn't happen). Return an empty response.
        return VerboseResponse(
            ticker=ticker, timeframe=timeframe,
            bars=(),
            latest_change=ChangeSignal.NO_CHANGE,
            computed_at=datetime.now(timezone.utc),
        )
    change = _classify_regime_transition(states)
    retained = states[-state_history_retain:] if state_history_retain < len(states) else states
    return VerboseResponse(
        ticker=ticker,
        timeframe=timeframe,
        bars=tuple(retained),
        latest_change=change,
        computed_at=datetime.now(timezone.utc),
    )


def compute_full_terse(
    *,
    ticker: TickerRef,
    history: list[OHLCVBar],
    latest_bar: OHLCVBar,
    timeframe: Timeframe,
    params: IndicatorParams,
) -> TerseResponse:
    """Same as verbose, but returns the terse summary only.

    Builds the full walk because we need the regime trajectory to
    classify the latest transition; the discarded states are
    cheap (~150 of them).
    """
    bars = list(history) + [latest_bar]
    states, _trajectory = _walk_bars(bars, params=params, timeframe=timeframe)
    if not states:
        return TerseResponse(
            ticker=ticker, timeframe=timeframe,
            latest_bar_timestamp=latest_bar.timestamp,
            latest_change=ChangeSignal.NO_CHANGE,
            latest_price=latest_bar.close,
            computed_at=datetime.now(timezone.utc),
        )
    change = _classify_regime_transition(states)
    latest = states[-1]
    return TerseResponse(
        ticker=ticker,
        timeframe=timeframe,
        latest_bar_timestamp=latest.timestamp,
        latest_change=change,
        latest_price=latest_bar.close,
        computed_at=datetime.now(timezone.utc),
    )


# ============================================================
# Helpers
# ============================================================


def _walk_bars(
    bars: list[OHLCVBar],
    *,
    params: IndicatorParams,
    timeframe: Timeframe,
) -> tuple[list[BarState], list[Optional[Regime]]]:
    """Walk the supplied bars with `compute_regime` and return one
    `BarState` per bar.

    Returns `(states, trajectory)` where `trajectory[i]` is the
    Regime computed for bar i (or None during warmup). `states[i]`
    is the corresponding BarState. Both lists have the same length
    as `bars`; bars in warmup contribute a `BarState` carrying NaN
    indicator values and `CoreRegime.NEUTRAL` for both core_bg
    and display_bg.
    """
    if not bars:
        return [], []

    closes = pd.Series([b.close for b in bars])
    volumes = pd.Series([b.volume for b in bars])
    highs = pd.Series([b.high for b in bars])
    lows = pd.Series([b.low for b in bars])
    series = compute_indicator_series(closes, volumes, highs, lows, params=params)

    states: list[BarState] = []
    trajectory: list[Optional[Regime]] = []

    prev_regime: Regime = Regime.NEUTRAL
    prev_close: Optional[float] = None
    prev_lower_env: Optional[float] = None
    prev_upper_env: Optional[float] = None

    for i, bar in enumerate(bars):
        med = series.emas.med.iloc[i]
        long_ = series.emas.long.iloc[i]
        longest = series.emas.longest.iloc[i]
        atr_val = series.atr.iloc[i]
        ui = series.bands.upper_inner.iloc[i]
        li = series.bands.lower_inner.iloc[i]
        um = series.bands.upper_mid.iloc[i]
        lm = series.bands.lower_mid.iloc[i]
        uo = series.bands.upper_outer.iloc[i]
        lo = series.bands.lower_outer.iloc[i]

        is_warm = all(
            isinstance(v, (int, float)) and not math.isnan(v) and math.isfinite(v)
            for v in (med, long_, longest, atr_val)
        )

        if not is_warm:
            # Warmup — emit a BarState placeholder, no regime.
            states.append(BarState(
                timestamp=bar.timestamp,
                med_ema=float(med) if isinstance(med, (int, float)) else float("nan"),
                long_ema=float(long_) if isinstance(long_, (int, float)) else float("nan"),
                longest_ema=float(longest) if isinstance(longest, (int, float)) else float("nan"),
                atr=float(atr_val) if isinstance(atr_val, (int, float)) else float("nan"),
                upper_inner=float(ui) if isinstance(ui, (int, float)) else float("nan"),
                lower_inner=float(li) if isinstance(li, (int, float)) else float("nan"),
                upper_mid=float(um) if isinstance(um, (int, float)) else float("nan"),
                lower_mid=float(lm) if isinstance(lm, (int, float)) else float("nan"),
                upper_outer=float(uo) if isinstance(uo, (int, float)) else float("nan"),
                lower_outer=float(lo) if isinstance(lo, (int, float)) else float("nan"),
                pierces_upper_mid=False,
                pierces_lower_mid=False,
                core_bg=CoreRegime.NEUTRAL,
                display_bg=CoreRegime.NEUTRAL,
            ))
            trajectory.append(None)
            continue

        upper_env = float(max(med, long_, longest))
        lower_env = float(min(med, long_, longest))
        bar_inputs = BarInputs(
            close=bar.close,
            upper_inner=float(ui), lower_inner=float(li),
            upper_outer=float(uo), lower_outer=float(lo),
            upper_envelope=upper_env, lower_envelope=lower_env,
        )
        current_regime = compute_regime(
            previous_regime=prev_regime,
            previous_close=prev_close,
            previous_lower_envelope=prev_lower_env,
            previous_upper_envelope=prev_upper_env,
            bar=bar_inputs,
        )

        states.append(BarState(
            timestamp=bar.timestamp,
            med_ema=float(med),
            long_ema=float(long_),
            longest_ema=float(longest),
            atr=float(atr_val),
            upper_inner=float(ui), lower_inner=float(li),
            upper_mid=float(um), lower_mid=float(lm),
            upper_outer=float(uo), lower_outer=float(lo),
            # Pierces — recomputable from this bar's high/low vs the
            # mid bands. Useful for the debug endpoint even though
            # the new regime spec doesn't gate on them.
            pierces_upper_mid=bar.high >= float(um),
            pierces_lower_mid=bar.low <= float(lm),
            # Under the unified regime model, core and display
            # carry the same value at the wire boundary.
            core_bg=CoreRegime(int(current_regime)),
            display_bg=CoreRegime(int(current_regime)),
        ))
        trajectory.append(current_regime)

        prev_regime = current_regime
        prev_close = bar.close
        prev_lower_env = lower_env
        prev_upper_env = upper_env

    return states, trajectory


def _classify_regime_transition(states: list[BarState]) -> ChangeSignal:
    """Map the regime delta between the last two states to a
    ChangeSignal.

    This is a debug-grade classifier — the production signal path
    (Step 5 backfill, Step 6 live) uses `generate_signal` with proper
    PositionState threading. Stateless compute has no prior state,
    so we just classify by the regime transition:

      - NEUTRAL → BULLISH (or BEARISH → BULLISH): BUY
      - NEUTRAL → BEARISH (or BULLISH → BEARISH): SELL
      - BULLISH → NEUTRAL: EXIT_FROM_BUY
      - BEARISH → NEUTRAL: EXIT_FROM_SELL
      - same → same: NO_CHANGE
    """
    if len(states) < 2:
        return ChangeSignal.NO_CHANGE
    prev_bg = states[-2].display_bg
    curr_bg = states[-1].display_bg
    if prev_bg == curr_bg:
        return ChangeSignal.NO_CHANGE
    if curr_bg == CoreRegime.BULLISH:
        return ChangeSignal.BUY
    if curr_bg == CoreRegime.BEARISH:
        return ChangeSignal.SELL
    # curr is NEUTRAL — exit from whichever side prev was.
    if prev_bg == CoreRegime.BULLISH:
        return ChangeSignal.EXIT_FROM_BUY
    if prev_bg == CoreRegime.BEARISH:
        return ChangeSignal.EXIT_FROM_SELL
    return ChangeSignal.NO_CHANGE
