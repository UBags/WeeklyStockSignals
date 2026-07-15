"""The per-signal stage sequence, defined once (spec §19-21 wiring).

Three stages, one order, two callers:

    enrichment (X)  ->  labels (y)  ->  ensemble (X)

backfill and the EOD cycle both call run_per_signal_stages. They differ only
in what they pass:

  * SCOPE — a list of ticker_ids, or None for "all". The three stages take
    independent scopes because they answer different questions:
      - enrichment + ensemble: the signals that need features NOW
        (backfill: the run's tickers; EOD: today's flipped tickers).
      - labels: the signals whose forward horizon may have matured
        (a DIFFERENT set — today's flips cannot be labelled yet, and the
        signals that CAN be finalised today flipped weeks ago).

  * run_phase — the resume-ledger wrapper. backfill passes its own binding
    (the real run_phase, or the scoped-bypass local when the run is
    ticker-scoped). The EOD cycle has no ledger and passes a passthrough.
    The sequencer must NOT import run_phase: importing it would bypass
    backfill's scoped-run ledger-bypass and let a scoped run write false
    COMPLETE markers.

Nothing here computes signals. The three runners own the calc + upsert; this
owns only the ORDER and the wrapping, so a change to the sequence is a change
in one place.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.engine import Engine

from .signal_enrichment_runner import backfill_signal_enrichment
from .signal_ensemble_runner import backfill_signal_ensemble_features
from .signal_labels_runner import backfill_signal_labels


@dataclass(frozen=True, slots=True)
class PerSignalStageResult:
    """What each stage returned, or None if the ledger skipped it (resume)."""

    enrichment: object | None
    labels: object | None
    ensemble: object | None


def _passthrough_run_phase(_db, _ex, _phase, fn, **_kwargs):
    """The EOD cycle's run_phase: no ledger, just run it. Signature matches
    phase_ledger.run_phase so the same sequencer body drives both callers."""
    return fn()


def run_per_signal_stages(
    db_engine: Engine,
    exchange: str,
    *,
    enrichment_scope: list[int] | None,
    labels_scope: list[int] | None,
    ensemble_scope: list[int] | None,
    run_phase: Callable = _passthrough_run_phase,
    progress_cb: Callable[[str, str], None] | None = None,
    backfill_run_id: int | None = None,
    force: bool = False,
    # Runners injected for testability — DI, not monkeypatch, consistent with
    # the seam pattern that retired this codebase's ~450 test patches. Callers
    # never pass these; a test does, to observe the sequence without a DB.
    _enrichment_runner: Callable = backfill_signal_enrichment,
    _labels_runner: Callable = backfill_signal_labels,
    _ensemble_runner: Callable = backfill_signal_ensemble_features,
) -> PerSignalStageResult:
    """Run enrichment -> labels -> ensemble, in that order.

    run_phase is INJECTED (see module docstring): backfill passes its live
    binding; the EOD path lets it default to the no-ledger passthrough. Each
    stage is wrapped identically to the hand-written backfill blocks this
    replaces — same phase names, same starting/done-or-skipped messages.
    """
    def _emit(stage: str, message: str) -> None:
        if progress_cb is not None:
            progress_cb(stage, message)

    # ---- enrichment (X): the causal per-signal feature vector ----
    _emit("stage_8b_signal_enrichment", "starting")
    enr = run_phase(
        db_engine, exchange, "signal_enrichment",
        lambda: _enrichment_runner(
            db_engine, exchange,
            scoped_tickers=enrichment_scope,
            progress_cb=progress_cb,
        ),
        backfill_run_id=backfill_run_id, force=force,
    )
    _emit(
        "stage_8b_signal_enrichment",
        "done" if enr is not None else "skipped (resume)",
    )

    # ---- labels (y): forward-return targets; finalises matured horizons ----
    _emit("stage_8c_signal_labels", "starting")
    lbl = run_phase(
        db_engine, exchange, "signal_labels",
        lambda: _labels_runner(
            db_engine, exchange,
            scoped_tickers=labels_scope,
            progress_cb=progress_cb,
        ),
        backfill_run_id=backfill_run_id, force=force,
    )
    _emit(
        "stage_8c_signal_labels",
        "done" if lbl is not None else "skipped (resume)",
    )

    # ---- ensemble (X): cross-industry leader confirmation (§17.5) ----
    _emit("stage_8d_signal_ensemble", "starting")
    sef = run_phase(
        db_engine, exchange, "signal_ensemble_features",
        lambda: _ensemble_runner(
            db_engine, exchange,
            scoped_tickers=ensemble_scope,
            progress_cb=progress_cb,
        ),
        backfill_run_id=backfill_run_id, force=force,
    )
    _emit(
        "stage_8d_signal_ensemble",
        "done" if sef is not None else "skipped (resume)",
    )

    return PerSignalStageResult(enrichment=enr, labels=lbl, ensemble=sef)
