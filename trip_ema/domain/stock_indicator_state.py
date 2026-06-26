"""StockIndicatorState — V1 lean per-ticker indicator state.

The "core" half of the Step 1 + Step 2 schema split:

  stock_indicator_state      ← 12 core columns. THIS dataclass.
  stock_indicator_enrichment ← 23 enrichment columns. Separate dataclass
                                (Step 5).

The dataclass intentionally carries ONLY the 12 core fields plus the
3 PK keys and the audit timestamp. Enrichment fields (pattern labels,
turn flags, distance metrics, etc.) live on the separate
StockIndicatorEnrichment dataclass and are written by a separate
runner stage (Step 5 of the clean-pipeline V1 refactor).

Contrast with the legacy `IndicatorState` dataclass in
`trip_ema.fundamentals`, which is the wide 38-field version coupled
to the legacy `indicator_state` table. The two dataclasses coexist
during the side-by-side validation period and the legacy one is
removed in Step 14.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


# Allowed values for stock_indicator_state.display_regime. The DB
# CHECK constraint enforces the same set; this constant is for
# application-level validation in the repository's upsert path.
REGIME_VALUES = (-1, 0, 1)


@dataclass(frozen=True, slots=True)
class StockIndicatorState:
    """One row from stock_indicator_state.

    PK: (ticker_id, timeframe, bar_timestamp).

    Field meanings (mirrors the Pine indicator):

    - `med_ema` / `long_ema` / `longest_ema`: the three EMAs.
      Computed with volume-weighted construction + Hull-WMA smoothing
      per the operator's spec. All three must be > 0.

    - `atr`: Average True Range over `params.atr_length` bars. Must
      be >= 0.

    - The six band values: derived from EMAs + ATR per
      `compute_atr_bands`. `upper_inner`/`lower_inner` track
      max/min(emas) +/- atr_bands.inner_multiplier * atr; `_mid` and
      `_outer` use larger multipliers.

    - `display_regime`: -1 / 0 / +1 from the canonical regime
      classifier (`trip_ema.regime.compute_regime`). Sticky-on-NaN
      semantics during warmup (carries the prior regime forward
      rather than dropping to NEUTRAL).

    - `computed_at`: when this row was emitted. Defaults to NOW() at
      the DB level on insert; callers can set it explicitly for
      deterministic-time tests.
    """
    # --- keys ---
    ticker_id: int
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
