"""Tier computation.

Recomputed weekly (CLI: `trip-ema-etl recompute-tiers`). Pure Python
input/output — the SQL query for "30-day ADV per ticker" lives in
the status_repo; the algorithm here just consumes the result.

Tier definition:
  - Tier 1: top N1 tickers by trailing-30-trading-day average daily
    value (volume * close), sorted descending.
  - Tier 2: next N2 tickers.
  - Tier 3: everything else (including new listings with no history).

Defaults: N1=200, N2=500. Configurable in config.yaml under
etl.tier_1_size / etl.tier_2_size.

Stability: the assignment is recomputed from scratch weekly. We don't
add hysteresis (e.g., "only promote if ADV exceeds threshold by
20%"). Liquidity is volatile enough that a name on the border can
legitimately flip between tier 1 and tier 2 week-to-week, and the
operational cost of being in tier 2 (waiting for EoD instead of
hourly intraday) is small enough not to warrant the complication.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable

from .types import Tier


@dataclass(frozen=True, slots=True)
class TickerAdv:
    """Input to the tiering algorithm: one row per ticker_id with its
    average daily turnover."""
    ticker_id: int
    avg_daily_value: float
    """Mean of (close * volume) over the lookback window. Tickers with
    no history get 0.0."""


@dataclass(frozen=True, slots=True)
class TierAssignment:
    ticker_id: int
    tier: Tier


def assign_tiers(
    ranked_inputs: Iterable[TickerAdv],
    *,
    tier_1_size: int = 200,
    tier_2_size: int = 500,
) -> list[TierAssignment]:
    """Rank tickers by avg_daily_value descending, then carve into
    three tiers by count.

    Ties at tier boundaries break by ticker_id ascending for determinism.
    A ticker with avg_daily_value=0 (no history) is always Tier 3,
    regardless of how many slots are nominally available — we don't
    let a name with literally no data jump into Tier 1 just because
    there are fewer than N1 tickers in the universe.

    Returns one TierAssignment per input.
    """
    inputs_list = list(ranked_inputs)
    # Sort: by avg_daily_value descending, then ticker_id ascending
    inputs_list.sort(key=lambda x: (-x.avg_daily_value, x.ticker_id))

    out: list[TierAssignment] = []
    for rank, item in enumerate(inputs_list):
        if item.avg_daily_value <= 0:
            out.append(TierAssignment(item.ticker_id, Tier.TIER_3))
            continue
        if rank < tier_1_size:
            out.append(TierAssignment(item.ticker_id, Tier.TIER_1))
        elif rank < tier_1_size + tier_2_size:
            out.append(TierAssignment(item.ticker_id, Tier.TIER_2))
        else:
            out.append(TierAssignment(item.ticker_id, Tier.TIER_3))
    return out
