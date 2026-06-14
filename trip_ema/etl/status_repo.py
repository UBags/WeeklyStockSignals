"""Repository for etl_runs and etl_ticker_status tables.

Two responsibilities:

  1. etl_runs lifecycle: open a row at the start of each CLI invocation,
     update its counters as work proceeds, close it on completion.
     The PySide GUI (Slice 2) reads this for the "ETL history" view.

  2. etl_ticker_status per-ticker bookkeeping: last successful bar
     date, last attempt time, consecutive_failures, parked flag.
     The runner uses this to decide when to park a ticker; the GUI
     uses it for the ticker table view.

Kept in one module because both tables are managed by the same
component (the runner) in the same transaction boundaries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .types import RunKind, RunOutcome

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TickerStatus:
    ticker_id: int
    last_successful_bar_date: Optional[date]
    last_attempt_at: Optional[datetime]
    consecutive_failures: int
    parked: bool
    parked_at: Optional[datetime]
    parked_reason: Optional[str]
    last_error: Optional[str]


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: int
    kind: str
    started_at: datetime
    finished_at: Optional[datetime]
    outcome: Optional[str]
    tickers_attempted: int
    tickers_succeeded: int
    tickers_failed: int
    bars_written: int
    correlation_id: Optional[str]
    notes: Optional[str]


class EtlStatusRepository:
    """All etl_runs and etl_ticker_status writes go through this.

    The Connection is supplied by the caller — the repo doesn't manage
    transactions. The runner opens one transaction per ETL phase
    (start-run, batch-of-tickers, finish-run) so partial work persists
    across crashes.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- etl_runs ---------- #

    def open_run(
        self,
        kind: RunKind,
        *,
        exchange: Optional[str] = None,
        correlation_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        """Insert a new etl_runs row with started_at=NOW(); return the
        run_id. finished_at and outcome stay NULL until close_run.

        Lifecycle ownership note (Phase 15a): the typical pattern is
        for one caller to own both `open_run` and `close_run` — the
        runner functions in this package, the signal-generation
        runner when called from the CLI, etc. The new HTTP signal-
        backtest route (Phase 15b) is the exception: it opens the
        row in the request thread (so a concurrent-run guard query
        can serialize new submissions) and hands the run_id to the
        background worker, which closes the row when work finishes
        or fails. If you write a similar split-ownership caller,
        document the split clearly — silent split ownership is hard
        to reason about.

        Args:
            kind: the RunKind discriminator.
            exchange: optional. New in Phase 15a — the etl_runs table
                now has an `exchange TEXT` column. Pass it when the
                caller knows which exchange the operation targets;
                leave None only for genuinely cross-exchange
                operations (none today; pre-Phase-15 rows remain
                NULL forever, which is correct because they weren't
                tagged at write time). All existing call sites in
                this package were updated in Phase 15a to pass it.
            correlation_id: optional correlation ID to thread through
                logs.
            notes: optional free-text note.
        """
        result = self._conn.execute(
            text("""
                INSERT INTO etl_runs (kind, exchange, correlation_id, notes)
                VALUES (:kind, :ex, :cid, :notes)
                RETURNING run_id
            """),
            {
                "kind": kind.value, "ex": exchange,
                "cid": correlation_id, "notes": notes,
            },
        )
        return result.scalar_one()

    def increment_counters(
        self,
        run_id: int,
        *,
        attempted: int = 0,
        succeeded: int = 0,
        failed: int = 0,
        bars_written: int = 0,
    ) -> None:
        """Bump per-batch counters. Multiple calls accumulate."""
        self._conn.execute(
            text("""
                UPDATE etl_runs SET
                    tickers_attempted = tickers_attempted + :att,
                    tickers_succeeded = tickers_succeeded + :suc,
                    tickers_failed    = tickers_failed    + :fail,
                    bars_written      = bars_written      + :bw
                WHERE run_id = :rid
            """),
            {
                "att": attempted, "suc": succeeded,
                "fail": failed, "bw": bars_written, "rid": run_id,
            },
        )

    def close_run(
        self,
        run_id: int,
        outcome: RunOutcome,
        *,
        notes: Optional[str] = None,
    ) -> None:
        """Set finished_at = NOW() and the outcome. If `notes` is given,
        appends to the existing notes (separated by a newline) — this
        is how the runner adds a divergence-count summary at the end."""
        self._conn.execute(
            text("""
                UPDATE etl_runs SET
                    finished_at = NOW(),
                    outcome     = :out,
                    notes       = CASE
                        WHEN :extra_notes IS NULL THEN notes
                        WHEN notes IS NULL THEN :extra_notes
                        ELSE notes || E'\n' || :extra_notes
                    END
                WHERE run_id = :rid
            """),
            {"out": outcome.value, "extra_notes": notes, "rid": run_id},
        )

    def cleanup_stale_runs(
        self,
        *,
        exchange: str,
        stale_after_hours: int = 6,
    ) -> int:
        """Close any etl_runs rows for `exchange` that look orphaned —
        no `outcome` and no `finished_at`, with `started_at` older
        than `stale_after_hours`.

        Why this exists. The etl_runs lifecycle is "open in one
        transaction, close in another". If the process dies mid-run
        (kill -9, OS reboot, crash), the row is left dangling with
        outcome=NULL and finished_at=NULL forever. Operators see
        these in the GUI / `trip-ema-etl status` and have to clear
        them manually. Worse, Phase 15b's signal_backtest
        concurrent-run guard reads `outcome IS NULL` as "still in
        flight" — a stale row would block all subsequent backtest
        submissions for that exchange until someone notices.

        Threshold. Default 6 hours. EoD typically finishes in ~15
        minutes; intraday in ~5; backfills can take an hour or two.
        6h is comfortably above the slowest legitimate case and well
        below the timescale at which a real orphan is operationally
        confusing. Operators can pass a longer value if they expect
        a multi-hour backfill to be running concurrently.

        Scope. Only rows for `exchange` are touched. Rows with
        `exchange IS NULL` (pre-Phase-15a, no longer produced by new
        code) are left alone — they're months old and don't interact
        with the per-exchange guards.

        Idempotency. Called many times safely. Each call writes the
        same UPDATE for the same matching rows; concurrent callers
        race harmlessly on the final write.

        Returns the number of rows closed. Caller is expected to
        log this if > 0.
        """
        result = self._conn.execute(
            text("""
                UPDATE etl_runs SET
                    finished_at = NOW(),
                    outcome     = 'failed',
                    notes       = COALESCE(notes || E'\n', '')
                                  || 'cleanup: orphaned by mid-run '
                                  || 'process death; auto-closed after '
                                  || :hrs || 'h'
                WHERE exchange = :ex
                  AND outcome IS NULL
                  AND finished_at IS NULL
                  AND started_at < NOW() - (:hrs || ' hours')::interval
            """),
            {"ex": exchange, "hrs": stale_after_hours},
        )
        return result.rowcount or 0

    def list_recent_runs(self, limit: int = 50) -> list[RunRecord]:
        """For the GUI's ETL-history tab."""
        result = self._conn.execute(
            text("""
                SELECT run_id, kind, started_at, finished_at, outcome,
                       tickers_attempted, tickers_succeeded, tickers_failed,
                       bars_written, correlation_id, notes
                FROM etl_runs
                ORDER BY started_at DESC
                LIMIT :lim
            """),
            {"lim": limit},
        )
        return [
            RunRecord(
                run_id=row.run_id,
                kind=row.kind,
                started_at=row.started_at,
                finished_at=row.finished_at,
                outcome=row.outcome,
                tickers_attempted=row.tickers_attempted,
                tickers_succeeded=row.tickers_succeeded,
                tickers_failed=row.tickers_failed,
                bars_written=row.bars_written,
                correlation_id=row.correlation_id,
                notes=row.notes,
            )
            for row in result
        ]

    # ---------- etl_ticker_status ---------- #

    def record_success(
        self,
        ticker_id: int,
        last_bar_date: Optional[date],
        *,
        now: Optional[datetime] = None,
    ) -> None:
        """Mark a ticker as successfully fetched. Resets
        consecutive_failures to 0 and clears parked state. Use
        INSERT ... ON CONFLICT so the first call for a new ticker
        creates the row."""
        ts = now or datetime.now(timezone.utc)
        self._conn.execute(
            text("""
                INSERT INTO etl_ticker_status
                    (ticker_id, last_successful_bar_date, last_attempt_at,
                     consecutive_failures, parked, parked_at, parked_reason,
                     last_error, updated_at)
                VALUES
                    (:tid, :lbd, :ts, 0, FALSE, NULL, NULL, NULL, :ts)
                ON CONFLICT (ticker_id) DO UPDATE SET
                    last_successful_bar_date =
                        COALESCE(EXCLUDED.last_successful_bar_date,
                                 etl_ticker_status.last_successful_bar_date),
                    last_attempt_at      = EXCLUDED.last_attempt_at,
                    consecutive_failures = 0,
                    parked               = FALSE,
                    parked_at            = NULL,
                    parked_reason        = NULL,
                    last_error           = NULL,
                    updated_at           = EXCLUDED.updated_at
            """),
            {"tid": ticker_id, "lbd": last_bar_date, "ts": ts},
        )

    def record_failure(
        self,
        ticker_id: int,
        error_message: str,
        *,
        park_threshold: int = 3,
        now: Optional[datetime] = None,
    ) -> bool:
        """Increment consecutive_failures. Returns True if the ticker
        is now parked (failure_count crossed park_threshold this
        call). The caller is responsible for appending to the parked
        CSV when True is returned."""
        ts = now or datetime.now(timezone.utc)
        # The UPDATE-OR-INSERT does the increment server-side so we
        # don't race two ETL runs against each other.
        result = self._conn.execute(
            text("""
                INSERT INTO etl_ticker_status
                    (ticker_id, last_attempt_at, consecutive_failures,
                     parked, parked_at, parked_reason, last_error, updated_at)
                VALUES
                    (:tid, :ts, 1, :park_now, :park_ts, :park_reason,
                     :err, :ts)
                ON CONFLICT (ticker_id) DO UPDATE SET
                    last_attempt_at      = EXCLUDED.last_attempt_at,
                    consecutive_failures = etl_ticker_status.consecutive_failures + 1,
                    parked               = (etl_ticker_status.consecutive_failures + 1) >= :park_threshold,
                    parked_at            = CASE
                        WHEN etl_ticker_status.parked = FALSE
                         AND (etl_ticker_status.consecutive_failures + 1) >= :park_threshold
                        THEN EXCLUDED.last_attempt_at
                        ELSE etl_ticker_status.parked_at
                    END,
                    parked_reason        = CASE
                        WHEN etl_ticker_status.parked = FALSE
                         AND (etl_ticker_status.consecutive_failures + 1) >= :park_threshold
                        THEN :park_reason
                        ELSE etl_ticker_status.parked_reason
                    END,
                    last_error           = EXCLUDED.last_error,
                    updated_at           = EXCLUDED.last_attempt_at
                RETURNING parked
            """),
            {
                "tid": ticker_id,
                "ts": ts,
                "err": error_message,
                "park_threshold": park_threshold,
                # The single-failure INSERT path won't trip the threshold
                # at park_threshold > 1, so park_now=False there.
                "park_now": park_threshold <= 1,
                "park_ts": ts if park_threshold <= 1 else None,
                "park_reason": (
                    f"{park_threshold} consecutive failures"
                    if park_threshold <= 1 else None
                ),
            },
        )
        return result.scalar_one()

    def get_status(self, ticker_id: int) -> Optional[TickerStatus]:
        result = self._conn.execute(
            text("""
                SELECT ticker_id, last_successful_bar_date, last_attempt_at,
                       consecutive_failures, parked, parked_at,
                       parked_reason, last_error
                FROM etl_ticker_status
                WHERE ticker_id = :tid
            """),
            {"tid": ticker_id},
        ).first()
        if result is None:
            return None
        return TickerStatus(
            ticker_id=result.ticker_id,
            last_successful_bar_date=result.last_successful_bar_date,
            last_attempt_at=result.last_attempt_at,
            consecutive_failures=result.consecutive_failures,
            parked=result.parked,
            parked_at=result.parked_at,
            parked_reason=result.parked_reason,
            last_error=result.last_error,
        )

    def list_parked(self) -> list[TickerStatus]:
        result = self._conn.execute(
            text("""
                SELECT ticker_id, last_successful_bar_date, last_attempt_at,
                       consecutive_failures, parked, parked_at,
                       parked_reason, last_error
                FROM etl_ticker_status
                WHERE parked = TRUE
                ORDER BY parked_at DESC NULLS LAST
            """),
        )
        return [
            TickerStatus(
                ticker_id=r.ticker_id,
                last_successful_bar_date=r.last_successful_bar_date,
                last_attempt_at=r.last_attempt_at,
                consecutive_failures=r.consecutive_failures,
                parked=r.parked,
                parked_at=r.parked_at,
                parked_reason=r.parked_reason,
                last_error=r.last_error,
            )
            for r in result
        ]

    def unpark(self, ticker_id: int) -> None:
        """Explicitly clear parked state without resetting failure
        count to 0 — that happens on the next successful fetch.
        Used by the `unpark` CLI to give a ticker another chance."""
        self._conn.execute(
            text("""
                UPDATE etl_ticker_status SET
                    parked = FALSE,
                    parked_at = NULL,
                    parked_reason = NULL,
                    updated_at = NOW()
                WHERE ticker_id = :tid
            """),
            {"tid": ticker_id},
        )

    def count_parked(self) -> int:
        """For the Prometheus gauge."""
        return self._conn.execute(
            text("SELECT COUNT(*) FROM etl_ticker_status WHERE parked = TRUE"),
        ).scalar() or 0
