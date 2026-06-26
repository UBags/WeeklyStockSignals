"""StockIndicatorEnrichment — V1 enrichment fields per ticker bar.

The "enrichment" half of the Step 1 + Step 2 schema split:

  stock_indicator_state      ← 12 core fields. Separate dataclass
                                (Step 4).
  stock_indicator_enrichment ← 23 enrichment fields. THIS dataclass.

Architectural intent: these fields are downstream of the core
indicator state and may evolve over time as the modeling work
progresses. Keeping them in a separate dataclass + table from the
core indicator state lets them change without touching the stable
core schema.

1:1 with `stock_indicator_state` via composite (ticker_id, timeframe,
bar_timestamp) — the DB FK with ON DELETE CASCADE enforces this.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Re-export so persistence-layer validation has one canonical source.
from trip_ema.fundamentals import PATTERN_LABELS

__all__ = ["PATTERN_LABELS", "StockIndicatorEnrichment"]


@dataclass(frozen=True, slots=True)
class StockIndicatorEnrichment:
    """One row from stock_indicator_enrichment.

    PK: (ticker_id, timeframe, bar_timestamp) — same composite as
    the core stock_indicator_state row this enrichment belongs to.

    Field meanings (mirrors the legacy IndicatorState's enrichment
    portion):

    - `pierces_upper_mid` / `pierces_lower_mid`: True when the
      bar's high/low straddled the upper or lower mid-band
      (high >= mid >= low intra-bar).

    - `dist_to_*_in_hl_units`: distance from close to each EMA,
      normalized by the rolling HL-range average. Nullable until
      the warmup period for that EMA completes.

    - `mean_dist_to_*_in_hl_units`: 52-period SMA of the above —
      "is this bar's price unusually close to / far from the EMA?"

    - `bars_since_*_turn`: bars elapsed since each EMA last
      changed slope direction. NULL until that EMA has had its
      first turn.

    - `pct_change_since_*_turn`: signed % change in close since
      each EMA's last direction change.

    - `*_pattern_label`: one of PATTERN_LABELS. The operator's
      "higher valleys / lower tops" pattern detection per EMA.

    - `*_turn_buy` / `*_turn_sell`: True on the bar where the EMA
      changes direction (buy = slope flipped -→+; sell = slope
      flipped +→-).

    - `computed_at`: when this row was emitted.
    """
    # --- keys ---
    ticker_id: int
    timeframe: str
    bar_timestamp: datetime

    # --- band-pierce flags (2) ---
    pierces_upper_mid: bool
    pierces_lower_mid: bool

    # --- per-EMA distance in HL units (3, nullable) ---
    dist_to_med_in_hl_units: float | None
    dist_to_long_in_hl_units: float | None
    dist_to_longest_in_hl_units: float | None

    # --- rolling mean of those distances (3, nullable) ---
    mean_dist_to_med_in_hl_units: float | None
    mean_dist_to_long_in_hl_units: float | None
    mean_dist_to_longest_in_hl_units: float | None

    # --- bars-since each EMA last turned (3, nullable) ---
    bars_since_med_turn: int | None
    bars_since_long_turn: int | None
    bars_since_longest_turn: int | None

    # --- pct-change-since each EMA last turned (3, nullable) ---
    pct_change_since_med_turn: float | None
    pct_change_since_long_turn: float | None
    pct_change_since_longest_turn: float | None

    # --- pattern labels per EMA (3, NOT NULL DEFAULT 'NA') ---
    med_pattern_label: str
    long_pattern_label: str
    longest_pattern_label: str

    # --- turn flags per EMA (6, NOT NULL DEFAULT FALSE) ---
    med_turn_buy: bool
    med_turn_sell: bool
    long_turn_buy: bool
    long_turn_sell: bool
    longest_turn_buy: bool
    longest_turn_sell: bool

    # --- audit ---
    computed_at: datetime
