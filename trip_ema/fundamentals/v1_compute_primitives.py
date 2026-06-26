"""V1 clean-pipeline compute primitives — CORE state only.

Produces the 12-core-field per-bar state for the new
`stock_indicator_state` / `industry_indicator_state` tables, with
NO enrichment fields. Enrichment fields are produced by a separate
function (Step 5).

Wraps the existing math layer (`compute_indicator_series`) and the
existing regime walker (`_compute_regime_series`) — no math is
duplicated; this module just emits a leaner per-bar dict.

Contrast with `compute_bar_states` (legacy, same module's stock):
that function emits all 38 fields including enrichment. The V1
pipeline calls compute_state_primitives only.
"""
from __future__ import annotations

import pandas as pd

from .indicator_primitives import (
    INDICATOR_DEFAULTS,
    EngineParams,
    _compute_regime_series,
    compute_indicator_series,
)


def compute_state_primitives(
    close: pd.Series,
    volume: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    trade_dates: pd.Series,
    params: EngineParams = INDICATOR_DEFAULTS,
    synthetic_atr_for_index: bool = False,
) -> list[dict]:
    """Walk the input bar series and emit one dict per bar with the
    12 core indicator fields (no enrichment).

    Per-bar dict keys:
      bar_timestamp,
      med_ema, long_ema, longest_ema,
      atr,
      upper_inner_band, lower_inner_band,
      upper_mid_band, lower_mid_band,
      upper_outer_band, lower_outer_band,
      display_regime

    `bar_timestamp` carries through whatever `trade_dates` holds —
    typically a `date`, `pd.Timestamp`, or `datetime`. The caller is
    responsible for converting to UTC TIMESTAMPTZ before persisting.

    Bars where any core indicator is NaN (warmup) are SKIPPED — the
    returned list will be shorter than the input series. The repo's
    NOT NULL constraints would reject these anyway and there's no
    business value in persisting a row with NaN math.

    Regime semantics on those skipped bars: the regime walker is
    sticky-on-NaN (carries prior regime forward), but since we don't
    emit the warmup bars at all, the question doesn't arise here.

    For industry indices (single-value series with synthetic OHLC),
    pass high=low=close=value, volume=1, and
    `synthetic_atr_for_index=True`. The math layer will use the
    operator's |Δclose|×√2 synthetic ATR formula.
    """
    if len(close) == 0:
        return []

    # Compute the math layer once.
    series = compute_indicator_series(
        close, volume, high, low,
        params=params,
        synthetic_atr_for_index=synthetic_atr_for_index,
    )
    # Walk the regime per bar.
    regime_series = _compute_regime_series(close, series)

    n = len(close)
    out: list[dict] = []
    for i in range(n):
        med_v = series.emas.med.iloc[i]
        long_v = series.emas.long.iloc[i]
        longest_v = series.emas.longest.iloc[i]
        atr_v = series.atr.iloc[i]

        # Skip warmup bars where any core value is NaN. The DB would
        # reject these via NOT NULL anyway.
        if (pd.isna(med_v) or pd.isna(long_v) or pd.isna(longest_v)
                or pd.isna(atr_v)):
            continue

        out.append({
            "bar_timestamp": trade_dates.iloc[i],
            "med_ema": float(med_v),
            "long_ema": float(long_v),
            "longest_ema": float(longest_v),
            "atr": float(atr_v),
            "upper_inner_band": float(series.bands.upper_inner.iloc[i]),
            "lower_inner_band": float(series.bands.lower_inner.iloc[i]),
            "upper_mid_band": float(series.bands.upper_mid.iloc[i]),
            "lower_mid_band": float(series.bands.lower_mid.iloc[i]),
            "upper_outer_band": float(series.bands.upper_outer.iloc[i]),
            "lower_outer_band": float(series.bands.lower_outer.iloc[i]),
            "display_regime": int(regime_series.iloc[i]),
        })

    return out
