"""Archived legacy polymorphic indicator row.

`IndicatorState` was one row of the pre-V1 `indicator_state` table — a
single wide, polymorphic shape holding BOTH ticker bar series
(ticker_id, timeframe) and industry indices (industry_name). That table
was dropped and the compute path split into the dedicated
stock_indicator_state / industry_indicator_state dataclasses on the
state-carry engine. Nothing in production references this dataclass; it
is retained only so tests/fundamentals/test_indicator_domain.py can pin
the legacy contract. Do not import from production code.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class IndicatorState:
    """One row from indicator_state.

    Polymorphic series identity: either (ticker_id, timeframe) for
    a ticker bar series, or industry_name for an industry index.
    Exactly one of the two is non-None — the DB CHECK constraint
    enforces this and the dataclass relies on application code
    constructing instances correctly.

    Most fields mirror the Pine Script. A few notes on the less-
    obvious ones:

    - `display_regime`: -1 / 0 / +1. core is the
      underlying state; display is the gated state that downstream
      strategy consumers actually read.

    - `dist_to_*_in_hl_units`: distance from price to the
      respective EMA, measured in units of the 26-period SMA of
      (high - low). The operator's table uses this as a
      normalized "how far from the EMA are we" metric.

    - `mean_dist_to_*_in_hl_units`: the 52-period SMA of the above.
      Used in the operator's table to colorize the current
      distance (green if below mean, red if above) — the idea
      being "is the price closer to the EMA than usual?"

    - `bars_since_*_turn`: bars since each EMA last changed slope
      direction. NULL until each EMA has had at least one turn.

    - `pct_change_since_*_turn`: signed percent change in close
      from the bar where each EMA last turned.

    - `*_turn_buy` / `*_turn_sell`: TRUE on the bar where the EMA
      changes direction (buy = slope flipped from -→+; sell =
      slope flipped from +→-). Used by signal aggregation.

    - `*_pattern_label`: one of PATTERN_LABELS. The operator's
      "higher valleys / lower tops" pattern detection per EMA.
    """
    # --- polymorphic identity ---
    ticker_id: int | None
    timeframe: str | None
    industry_name: str | None
    trade_date: date

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

    # --- pierces ---
    pierces_upper_mid: bool
    pierces_lower_mid: bool

    # --- per-EMA distances ---
    dist_to_med_in_hl_units: float | None
    dist_to_long_in_hl_units: float | None
    dist_to_longest_in_hl_units: float | None
    mean_dist_to_med_in_hl_units: float | None
    mean_dist_to_long_in_hl_units: float | None
    mean_dist_to_longest_in_hl_units: float | None

    # --- bars-since + pct-change-since per EMA ---
    bars_since_med_turn: int | None
    bars_since_long_turn: int | None
    bars_since_longest_turn: int | None
    pct_change_since_med_turn: float | None
    pct_change_since_long_turn: float | None
    pct_change_since_longest_turn: float | None

    # --- pattern labels ---
    med_pattern_label: str
    long_pattern_label: str
    longest_pattern_label: str

    # --- raw turn signals at this bar ---
    med_turn_buy: bool
    med_turn_sell: bool
    long_turn_buy: bool
    long_turn_sell: bool
    longest_turn_buy: bool
    longest_turn_sell: bool

    # --- audit ---
    computed_at: datetime
