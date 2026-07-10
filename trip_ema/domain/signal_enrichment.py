"""Domain object for a per-signal enrichment feature-store row (§19)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class SignalEnrichment:
    """One materialised signal's feature vector + (later) its label.

    `features` is the evolving Appendix-A vector (stored as JSONB).
    Natural key: (ticker_id, timeframe, signal_date, signal_type).
    The forward-return labels are the training target y and stay None
    until an offline labelling pass fills them.
    """

    ticker_id: int
    timeframe: str
    signal_date: date
    signal_type: str
    features: dict
    feature_spec_version: str
    industry_id: int | None = None
