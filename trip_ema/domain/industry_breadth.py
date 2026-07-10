"""Per-industry weekly regime proxy g^I (spec §17.1)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class IndustryBreadth:
    industry_name: str
    asof_date: date
    breadth: float
