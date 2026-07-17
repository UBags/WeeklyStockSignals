"""Shared industry-stage execution over the fundamentals core.

One mode-parameterized function per industry stage, used by BOTH orchestrators
(recompute_market = incremental, backfill = full). Neither orchestrator owns the
other's stage; both depend on this shared layer. Merged from the former
_stage_industry_state_{incremental,full} / _stage_industry_enrichment_{...} pairs.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from ..fundamentals.v1_runners import (
    populate_industry_indicator_enrichment_for_all,
    populate_industry_indicator_state_for_all,
)


def _stage_industry_state(
    db_engine: Engine, *, mode: str, n_weeks_to_upsert: int | None = None,
    progress_cb=None, industries: list[str] | None = None,
) -> tuple[int, int, int]:
    """industry_indicator_state for all industries. mode="incremental" (live,
    trailing n_weeks) or mode="full" (backfill, all history). Returns
    (succeeded, failed, skipped) counts."""
    kwargs = {"n_weeks_to_upsert": n_weeks_to_upsert} if mode == "incremental" else {}
    report = populate_industry_indicator_state_for_all(
        db_engine, mode=mode, progress_cb=progress_cb, industries=industries,
        **kwargs,
    )
    return (
        len(report.get("succeeded", [])),
        len(report.get("failed", [])),
        len(report.get("skipped", [])),
    )


def _stage_industry_enrichment(
    db_engine: Engine, *, mode: str, n_weeks_to_upsert: int | None = None,
    progress_cb=None, industries: list[str] | None = None,
) -> tuple[int, int, int]:
    """industry_indicator_enrichment for all industries. See _stage_industry_state
    for the mode semantics. Returns (succeeded, failed, skipped) counts."""
    kwargs = {"n_weeks_to_upsert": n_weeks_to_upsert} if mode == "incremental" else {}
    report = populate_industry_indicator_enrichment_for_all(
        db_engine, mode=mode, progress_cb=progress_cb, industries=industries,
        **kwargs,
    )
    return (
        len(report.get("succeeded", [])),
        len(report.get("failed", [])),
        len(report.get("skipped", [])),
    )
