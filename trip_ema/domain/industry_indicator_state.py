"""IndustryIndicatorState — V1 lean per-industry indicator state.

The industry-side equivalent of `StockIndicatorState`. Same 11
non-PK fields (EMAs + ATR + 6 bands + display_regime), same audit
column, same validation rules.

Differences from StockIndicatorState:
  - PK uses `industry_name: str` instead of `ticker_id: int`
  - The math is computed on weekly bars aggregated from
    industry_index_value daily rows (Phase 1B unification —
    daily_to_weekly with tz='Asia/Kolkata').
  - The ATR is true-range, not synthetic — see
    populate_industry_indicator_state_for_industry which passes
    synthetic_atr_for_index=False to compute_state_primitives.

Like StockIndicatorState, this dataclass carries ONLY the 12 core
fields plus PK + audit. Enrichment fields (pattern labels, turn
flags, distance metrics) live on a separate dataclass
`IndustryIndicatorEnrichment` (Step 8).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


# Allowed values for industry_indicator_state.display_regime. The
# DB CHECK constraint enforces the same set; this constant is for
# application-level validation in the repository's upsert path.
REGIME_VALUES = (-1, 0, 1)


@dataclass(frozen=True, slots=True)
class IndustryIndicatorState:
    """One row from industry_indicator_state.

    PK: (industry_name, timeframe, bar_timestamp).

    Field meanings mirror StockIndicatorState exactly — the
    underlying math layer is the same `compute_state_primitives`,
    just walked over IIV-derived weekly bars instead of OHLCV bars.

    - `med_ema` / `long_ema` / `longest_ema`: three EMAs computed
      from the weekly close (= last IIV value in the week). All
      three must be > 0.

    - `atr`: True-range ATR over the weekly bars, where the bar's
      H/L are max/min of the IIV daily values in that week.

    - The six band values: derived from EMAs + ATR.

    - `display_regime`: -1 / 0 / +1 from the canonical regime
      classifier.

    - `computed_at`: when this row was emitted. Defaults to NOW()
      at the DB level on insert; callers can set it explicitly for
      deterministic-time tests.
    """
    # --- keys ---
    industry_name: str
    timeframe: str
    bar_timestamp: datetime

    # --- EMAs ---
    med_ema: float
    long_ema: float
    longest_ema: float

    # --- ATR + bands ---
    atr: float
    upper_inner_band: float
    lower_inner_band: float
    upper_mid_band: float
    lower_mid_band: float
    upper_outer_band: float
    lower_outer_band: float

    # --- regime ---
    display_regime: int

    # --- audit ---
    computed_at: datetime
