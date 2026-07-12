"""Repository for compute_runs — the activity log of orchestrator
ticks + backfill invocations.

Step 13a: surfaces "what's been running on this NUC" for the ETL
Status GUI's Activity Log panel. Each row is one scheduled tick
(intraday or eod) OR one backfill invocation.

Lifecycle:
    repo = ComputeRunsRepository(conn)
    run_id = repo.start(kind="intraday", exchange="NSE")
    # ... do the work ...
    repo.finish(
        run_id,
        tickers_attempted=2667,
        tickers_fetched=2496,
        failed_stage=None,
        error_message=None,
    )

A row left with `finished_at IS NULL` means the run crashed mid-way
or the process was killed. The `recent()` query returns these as
"in-flight" rows; the activity log surfaces them with a duration of
"running" so the operator can spot stuck runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True, slots=True)
class ComputeRunRow:
    """One row of the activity log. `finished_at = None` means the
    run is still in flight (or crashed without finishing)."""
    compute_run_id: int
    kind: str
    exchange: str
    started_at: datetime
    finished_at: datetime | None
    tickers_attempted: int | None
    tickers_fetched: int | None
    failed_stage: str | None
    error_message: str | None
    notes: str | None
    # PHASE-3F: internal stage token of the currently-running stage
    # (e.g. 'fetch_bars', 'iiv', 'notify'); None when no stage is
    # active. Lets the ETL Status GUI surface progress for
    # scheduler-driven runs that have no stdout channel.
    current_stage: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock duration, in seconds. None if still in-flight."""
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def is_in_flight(self) -> bool:
        return self.finished_at is None

    @property
    def is_success(self) -> bool:
        """True if the run finished AND no failure was recorded."""
        return (
            self.finished_at is not None
            and self.failed_stage is None
            and self.error_message is None
        )


_VALID_KINDS = frozenset({"intraday", "eod", "backfill"})


class ComputeRunsRepository:
    """Thin wrapper around the `compute_runs` table.

    Designed for inside-transaction use: callers pass an open
    `Connection` and commit/rollback themselves. No engine reference
    held internally — the repo is cheap to construct (no I/O).
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def start(
        self, *, kind: str, exchange: str, notes: str | None = None,
    ) -> int:
        """Insert a new compute_run row with finished_at = NULL.
        Returns the new compute_run_id.

        `kind` must be one of 'intraday' / 'eod' / 'backfill' — the
        CHECK constraint enforces this DB-side; this method also
        validates Python-side for a friendlier error message.
        """
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(_VALID_KINDS)}, "
                f"got {kind!r}"
            )
        row = self._conn.execute(
            text("""
                INSERT INTO compute_runs (kind, exchange, notes)
                     VALUES (:kind, :exchange, :notes)
                  RETURNING compute_run_id
            """),
            {"kind": kind, "exchange": exchange, "notes": notes},
        ).first()
        return int(row.compute_run_id)

    def finish(
        self,
        compute_run_id: int,
        *,
        tickers_attempted: int | None = None,
        tickers_fetched: int | None = None,
        failed_stage: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Mark the run finished. Sets finished_at = NOW() server-
        side so we don't depend on the client clock matching the
        server clock.

        It's safe to call this on a row that's already finished —
        the UPDATE will succeed but the started_at-based duration
        calculation will use the LATEST finished_at. Callers should
        avoid double-finishing.
        """
        self._conn.execute(
            text("""
                UPDATE compute_runs
                   SET finished_at       = NOW(),
                       tickers_attempted = :tickers_attempted,
                       tickers_fetched   = :tickers_fetched,
                       failed_stage      = :failed_stage,
                       error_message     = :error_message,
                       current_stage     = NULL
                 WHERE compute_run_id = :rid
            """),
            {
                "rid": compute_run_id,
                "tickers_attempted": tickers_attempted,
                "tickers_fetched": tickers_fetched,
                "failed_stage": failed_stage,
                "error_message": error_message,
            },
        )

    def set_current_stage(
        self, compute_run_id: int, stage: str | None,
    ) -> None:
        """PHASE-3F: publish the currently-running internal stage token
        for an in-flight run, so the ETL Status GUI can surface live
        progress for scheduler-driven (auto) runs — which, unlike
        GUI-spawned runs, have no stdout channel the GUI can read.

        `stage` is the internal token (e.g. 'fetch_bars', 'iiv',
        'notify'); pass None to clear. Best-effort at the call site —
        callers must not let a failure here abort the compute.
        """
        self._conn.execute(
            text("""
                UPDATE compute_runs
                   SET current_stage = :stage
                 WHERE compute_run_id = :rid
            """),
            {"rid": compute_run_id, "stage": stage},
        )

    def recent(
        self, n: int = 15, *, exchange: str | None = None,
    ) -> list[ComputeRunRow]:
        """Return the N most recent compute_run rows, newest first.

        `exchange=None` returns rows across all exchanges (current
        single-NSE deployment never sees more than one exchange).
        """
        if n <= 0:
            return []
        params: dict = {"n": n}
        sql = """
            SELECT compute_run_id, kind, exchange, started_at,
                   finished_at, tickers_attempted, tickers_fetched,
                   failed_stage, error_message, notes,
                   current_stage
              FROM compute_runs
        """
        if exchange is not None:
            sql += " WHERE exchange = :ex"
            params["ex"] = exchange
        # Step 13a fixup: compute_run_id DESC tiebreaker.
        # When multiple inserts share started_at (same SQL
        # transaction → identical NOW()), the PK provides
        # a stable secondary sort. Newer PK = newer row.
        sql += (
            " ORDER BY started_at DESC, "
            "compute_run_id DESC LIMIT :n"
        )

        rows = self._conn.execute(text(sql), params).all()
        return [_row_to_dc(r) for r in rows]

    def get(self, compute_run_id: int) -> ComputeRunRow | None:
        """Single-row lookup. Returns None if missing."""
        row = self._conn.execute(
            text("""
                SELECT compute_run_id, kind, exchange, started_at,
                       finished_at, tickers_attempted, tickers_fetched,
                       failed_stage, error_message, notes,
                       current_stage
                  FROM compute_runs
                 WHERE compute_run_id = :rid
            """),
            {"rid": compute_run_id},
        ).first()
        return _row_to_dc(row) if row else None

    def in_flight(
        self, *, exchange: str | None = None,
    ) -> list[ComputeRunRow]:
        """Rows with finished_at IS NULL — runs in progress OR runs
        that crashed without finishing. Backed by the partial index
        ix_compute_runs_in_flight."""
        params: dict = {}
        sql = """
            SELECT compute_run_id, kind, exchange, started_at,
                   finished_at, tickers_attempted, tickers_fetched,
                   failed_stage, error_message, notes,
                   current_stage
              FROM compute_runs
             WHERE finished_at IS NULL
        """
        if exchange is not None:
            sql += " AND exchange = :ex"
            params["ex"] = exchange
        # Step 13a fixup: see recent() for the tiebreaker
        # rationale.
        sql += " ORDER BY started_at DESC, compute_run_id DESC"
        rows = self._conn.execute(text(sql), params).all()
        return [_row_to_dc(r) for r in rows]


def _row_to_dc(row) -> ComputeRunRow:
    return ComputeRunRow(
        compute_run_id=int(row.compute_run_id),
        kind=row.kind,
        exchange=row.exchange,
        started_at=row.started_at,
        finished_at=row.finished_at,
        tickers_attempted=(
            int(row.tickers_attempted)
            if row.tickers_attempted is not None else None
        ),
        tickers_fetched=(
            int(row.tickers_fetched)
            if row.tickers_fetched is not None else None
        ),
        failed_stage=row.failed_stage,
        error_message=row.error_message,
        notes=row.notes,
        current_stage=row.current_stage,  # PHASE-3F
    )
