"""Influential-industry ensemble member (spec §17.4)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class EnsembleMember:
    industry_i: str
    member_j: str
    variant: str
    corr: float
    best_lag: int
    is_leader: bool
    sign: int
    asof_date: date
