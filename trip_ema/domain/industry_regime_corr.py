"""Industry x industry regime-correlation row (spec §17.3)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class IndustryRegimeCorr:
    industry_i: str
    industry_j: str
    window: int
    asof_date: date
    corr: float
    best_lag: int
    lead_lag_corr: float
    n_obs: int
