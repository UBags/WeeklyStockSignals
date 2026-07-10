"""Per ticker x week sector-relative metric row (tier-3, spec §16.4)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class SectorStockMetric:
    ticker_id: int
    asof_date: date
    window: int
    industry_name: str
    rs_rank: float | None
