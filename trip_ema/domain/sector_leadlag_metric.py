"""Per ticker x week x window sector lead/lag metric row (spec §16.4)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class SectorLeadlagMetric:
    ticker_id: int
    asof_date: date
    window: int
    industry_name: str
    is_constituent: bool
    insync_corr: float | None
    best_lag: int | None
    lead_lag_corr: float | None
    beta_sector: float | None
    n_obs: int | None
    coupling_class: str | None
