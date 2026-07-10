"""Phase ledger — crash-durable resume for the backfill pipeline.

The backfill runs for hours across many phases (fetch, state, enrichment,
market-cap, industry-index build, signals, ...). If it's interrupted partway,
a re-run must resume from where it stopped rather than redo everything or —
worse — skip a half-finished phase. This module provides that.

Contract (the correctness rule that makes resume safe):

    A phase is marked COMPLETE **only after** its function returns successfully.

So the four possible ledger states mean exactly:
  - no row          -> phase never started    -> run it
  - RUNNING         -> phase started, didn't finish (crash) -> re-run it
  - FAILED          -> phase raised last time  -> re-run it
  - COMPLETE        -> phase finished cleanly   -> skip it (unless force)

Because COMPLETE is written only on success, a crash mid-phase can only leave
RUNNING — never COMPLETE — so a half-done phase is always re-run. That is the
whole point: no half-baked phase is ever mistaken for done.

Granularity is per-phase: a crash 60% through a phase re-runs the whole phase.
Phases must therefore be idempotent (re-running recomputes the same result) —
which the backfill stages already are (upserts, delete-then-insert per day).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TypeVar
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..persistence import transaction

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Status constants.
RUNNING = "RUNNING"
COMPLETE = "COMPLETE"
FAILED = "FAILED"


def phase_status(
    db_engine: Engine, exchange: str, phase_name: str,
) -> str | None:
    """Current ledger status for (exchange, phase), or None if no row."""
    with db_engine.connect() as conn:
        return conn.execute(text("""
            SELECT status FROM pipeline_phase_status
            WHERE exchange = :ex AND phase_name = :ph
        """), {"ex": exchange, "ph": phase_name}).scalar()


def _mark(
    db_engine: Engine, exchange: str, phase_name: str, status: str,
    *, backfill_run_id: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    rows_affected: int | None = None,
    last_error: str | None = None,
) -> None:
    """Upsert the ledger row for (exchange, phase)."""
    with transaction(db_engine) as conn:
        conn.execute(text("""
            INSERT INTO pipeline_phase_status
                (exchange, phase_name, status, backfill_run_id,
                 started_at, completed_at, rows_affected, last_error,
                 updated_at)
            VALUES (:ex, :ph, :st, :rid, :sa, :ca, :ra, :err, NOW())
            ON CONFLICT (exchange, phase_name) DO UPDATE SET
                status = EXCLUDED.status,
                backfill_run_id = EXCLUDED.backfill_run_id,
                started_at = COALESCE(
                    EXCLUDED.started_at, pipeline_phase_status.started_at),
                completed_at = EXCLUDED.completed_at,
                rows_affected = EXCLUDED.rows_affected,
                last_error = EXCLUDED.last_error,
                updated_at = NOW()
        """), {
            "ex": exchange, "ph": phase_name, "st": status,
            "rid": backfill_run_id, "sa": started_at, "ca": completed_at,
            "ra": rows_affected, "err": last_error,
        })


def reset_phases(db_engine: Engine, exchange: str) -> int:
    """Clear all ledger rows for an exchange — forces a full rebuild on the
    next run. Returns rows deleted. Used by a --rebuild operator flag."""
    with transaction(db_engine) as conn:
        res = conn.execute(text("""
            DELETE FROM pipeline_phase_status WHERE exchange = :ex
        """), {"ex": exchange})
        return res.rowcount or 0


def run_phase(
    db_engine: Engine,
    exchange: str,
    phase_name: str,
    fn: Callable[[], T],
    *,
    backfill_run_id: int | None = None,
    force: bool = False,
    rows_of: Callable[[T], int] | None = None,
) -> T | None:
    """Run one pipeline phase under the resume ledger.

    If the phase is already COMPLETE (and not `force`), skip it and return
    None. Otherwise mark RUNNING, call `fn()`, and on success mark COMPLETE
    (recording rows_affected via `rows_of(result)` if provided). On exception,
    mark FAILED and re-raise — the caller's own try/except decides what to do,
    but the ledger records the phase as not-done so a re-run retries it.

    COMPLETE is written ONLY after fn() returns — a crash inside fn() leaves
    the row RUNNING, so the next run re-executes this phase.

    Returns fn()'s result, or None if the phase was skipped.
    """
    if not force:
        st = phase_status(db_engine, exchange, phase_name)
        if st == COMPLETE:
            logger.info(
                "phase %s/%s already COMPLETE — skipping", exchange, phase_name,
            )
            return None

    started = datetime.now(UTC)
    _mark(
        db_engine, exchange, phase_name, RUNNING,
        backfill_run_id=backfill_run_id, started_at=started,
    )
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 — record then re-raise
        _mark(
            db_engine, exchange, phase_name, FAILED,
            backfill_run_id=backfill_run_id, started_at=started,
            last_error=f"{type(e).__name__}: {e}"[:2000],
        )
        raise
    rows = None
    if rows_of is not None:
        try:
            rows = rows_of(result)
        except Exception:  # noqa: BLE001 — rows accounting is best-effort
            rows = None
    failed_items = getattr(result, "failed", None)
    if failed_items:
        _mark(
            db_engine, exchange, phase_name, FAILED,
            backfill_run_id=backfill_run_id, started_at=started,
            last_error=f"{len(failed_items)} item(s) failed"[:2000],
        )
        logger.warning(
            "phase %s/%s recorded FAILED: %d item(s) failed - resume "
            "will retry", exchange, phase_name, len(failed_items),
        )
        return result
    _mark(
        db_engine, exchange, phase_name, COMPLETE,
        backfill_run_id=backfill_run_id, started_at=started,
        completed_at=datetime.now(UTC), rows_affected=rows,
    )
    logger.info("phase %s/%s COMPLETE (rows=%s)", exchange, phase_name, rows)
    return result
