"""IndustryIndicatorEnrichment — V1 per-industry enrichment fields.

Industry-side mirror of `StockIndicatorEnrichment`. Same 23
enrichment fields, same PK shape (with `industry_name: str`
replacing `ticker_id: int`).

1:1 with `industry_indicator_state` via composite (industry_name,
timeframe, bar_timestamp) — the DB FK with ON DELETE CASCADE
enforces this. The runner is responsible for sequencing: state
populator (Step 7) must run before this enrichment populator
(Step 8) for each industry.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Re-export so persistence-layer validation has one canonical source.
from trip_ema.fundamentals import PATTERN_LABELS

__all__ = ["PATTERN_LABELS", "IndustryIndicatorEnrichment"]


@dataclass(frozen=True, slots=True)
class IndustryIndicatorEnrichment:
    """One row from industry_indicator_enrichment.

    PK: (industry_name, timeframe, bar_timestamp) — same composite
    as the core industry_indicator_state row this enrichment belongs
    to.

    Field meanings exactly mirror StockIndicatorEnrichment — see
    that dataclass for prose.
    """
    # --- keys ---
    industry_name: str
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
