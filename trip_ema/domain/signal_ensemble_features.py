"""Per-signal ensemble features (spec §17.5)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SignalEnsembleFeatures:
    signal_enrichment_id: int
    variant: str
    ensemble_score: float
    ensemble_confirms: bool
    ensemble_agreement: float
    n_leaders_bull: int
    n_leaders_bear: int
