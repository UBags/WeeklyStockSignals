"""V1 BackfillRunsRepository — the active-marker and audit log for
backfill invocations.

Per V1 cutover Step 11a's design:
  - Each `backfill` CLI invocation INSERTs one row at start and
    UPDATEs `finished_at` at end (success or failure)
  - The live cycle reads `is_running()` on every tick and defers
    when the answer is True
  - The unique partial index ensures at most ONE active row;
    accidental concurrent backfill starts fail with IntegrityError
  - `release_locks()` is the manual cleanup for orphan rows left
    behind by crashed backfill processes (no auto-detection via
    PID liveness — operator's call, not the system's)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True, slots=True)
class BackfillRunRecord:
    """One row of `backfill_runs`. Immutable snapshot; mutations go
    through the repo."""
    run_id: int
    started_at: datetime
    finished_at: datetime | None
    pid: int
    exchange: str
    date_from: date
    date_to: date
    refetch_bars: bool
    ticker_scope: str   # 'ALL' or comma-separated symbols


class BackfillRunsRepository:
    """Reads + writes for the `backfill_runs` marker table."""

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- writes ---------- #

    def start(
        self, *,
        pid: int,
        exchange: str,
        date_from: date,
        date_to: date,
        refetch_bars: bool,
        ticker_scope: str,
    ) -> int:
        """Insert a new active backfill_runs row (finished_at NULL).

        Raises `sqlalchemy.exc.IntegrityError` (unique violation on
        `uq_backfill_runs_one_active`) if another row already has
        `finished_at IS NULL`. Caller catches this and either fails
        loud or directs the operator to `--release-lock`.

        Returns the new `run_id`. Caller must call `finish(run_id)`
        when the backfill completes (typically in a try/finally).
        """
        row = self._conn.execute(
            text("""
                INSERT INTO backfill_runs (
                    pid, exchange, date_from, date_to,
                    refetch_bars, ticker_scope
                ) VALUES (
                    :pid, :ex, :df, :dt, :rb, :ts
                )
                RETURNING run_id
            """),
            {
                "pid": pid, "ex": exchange,
                "df": date_from, "dt": date_to,
                "rb": refetch_bars, "ts": ticker_scope,
            },
        ).first()
        if row is None:
            raise RuntimeError(
                "INSERT INTO backfill_runs returned no row"
            )
        return int(row.run_id)

    def finish(
        self, run_id: int, *, when: datetime | None = None,
    ) -> bool:
        """Mark the run finished. `when` defaults to NOW() in
        Postgres (we use a COALESCE to honor the column default).

        Returns True if a row was updated, False if no matching
        unfinished row existed (idempotent-on-repeat).
        """
        result = self._conn.execute(
            text("""
                UPDATE backfill_runs
                   SET finished_at = COALESCE(:when, NOW())
                 WHERE run_id = :rid
                   AND finished_at IS NULL
            """),
            {"rid": run_id, "when": when},
        )
        return result.rowcount == 1

    def release_locks(self) -> int:
        """UPDATE all unfinished rows to finished_at = NOW().

        Returns the count affected. Use only after confirming that
        no backfill process is actually running. Typically invoked
        via `backfill --release-lock` (Step 11b's CLI).
        """
        result = self._conn.execute(
            text("""
                UPDATE backfill_runs
                   SET finished_at = NOW()
                 WHERE finished_at IS NULL
            """),
        )
        return result.rowcount

    # ---------- reads ---------- #

    def is_running(self) -> bool:
        """True if any row has `finished_at IS NULL`. Cheap —
        indexed by the partial index `idx_backfill_runs_active`.

        Called on every orchestrator tick (~4x daily NSE), so cost
        matters; one indexed EXISTS query is sub-millisecond.
        """
        return bool(self._conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM backfill_runs
                     WHERE finished_at IS NULL
                )
            """),
        ).scalar_one())

    def active_runs(self) -> list[BackfillRunRecord]:
        """All unfinished rows. At most one given the unique
        partial index, but list-returning keeps the API defensive
        in case the constraint is dropped or relaxed later.

        Used by `/healthz` (Step 11b) to surface "backfill in
        progress" status in the readiness JSON.
        """
        rows = self._conn.execute(
            text("""
                SELECT run_id, started_at, finished_at, pid,
                       exchange, date_from, date_to,
                       refetch_bars, ticker_scope
                  FROM backfill_runs
                 WHERE finished_at IS NULL
                 ORDER BY started_at DESC
            """),
        ).all()
        return [_row_to_record(r) for r in rows]

    def get(self, run_id: int) -> BackfillRunRecord | None:
        """Fetch one run by id. Useful for tests and post-mortem
        inspection ('what happened in run 17?')."""
        row = self._conn.execute(
            text("""
                SELECT run_id, started_at, finished_at, pid,
                       exchange, date_from, date_to,
                       refetch_bars, ticker_scope
                  FROM backfill_runs
                 WHERE run_id = :rid
            """),
            {"rid": run_id},
        ).first()
        if row is None:
            return None
        return _row_to_record(row)


def _row_to_record(row) -> BackfillRunRecord:
    return BackfillRunRecord(
        run_id=int(row.run_id),
        started_at=row.started_at,
        finished_at=row.finished_at,
        pid=int(row.pid),
        exchange=row.exchange,
        date_from=row.date_from,
        date_to=row.date_to,
        refetch_bars=bool(row.refetch_bars),
        ticker_scope=row.ticker_scope,
    )
