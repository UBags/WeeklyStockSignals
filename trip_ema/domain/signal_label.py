"""Per-signal performance labels (target y). Retrospective — needs bars
after the signal — so kept separate from the causal feature store."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SignalLabel:
    signal_enrichment_id: int
    fwd_ret_4w: float | None
    fwd_ret_8w: float | None
    fwd_ret_13w: float | None
    fwd_max_dd: float | None
    fwd_rr: float | None
    quality_class: str | None
    horizon_complete: bool
