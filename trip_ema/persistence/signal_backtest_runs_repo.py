"""Repository for the signal_backtest_runs table (Phase 15a).

Per-(exchange, ticker_id, timeframe) resume tracking for backtest
signal generation. One row per (exchange, ticker, timeframe) ever
computed; the signal generator reads `computed_through_date` to
decide where to pick up on incremental runs and inserts/updates
the row after a successful per-ticker replay.

The `force_full=true` operator workflow calls `delete_by_exchange`
in a small dedicated transaction at run start. Subsequent replay
then sees empty resume state and recomputes from each ticker's
earliest bar.

Connection-injected, no transaction management. Matches the
contract of `EtlStatusRepository` and `ExchangeStateRepository`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain import Timeframe


@dataclass(frozen=True, slots=True)
class SignalBacktestRun:
    """A single row from signal_backtest_runs."""
    exchange: str
    ticker_id: int
    timeframe: Timeframe
    computed_from_date: date
    computed_through_date: date
    computed_at: datetime
    last_run_id: int | None


def _row_to_record(row: Any) -> SignalBacktestRun:
    return SignalBacktestRun(
        exchange=row.exchange,
        ticker_id=row.ticker_id,
        timeframe=Timeframe(row.timeframe),
        computed_from_date=row.computed_from_date,
        computed_through_date=row.computed_through_date,
        computed_at=row.computed_at,
        last_run_id=row.last_run_id,
    )


class SignalBacktestRunsRepository:
    """Reads + writes against signal_backtest_runs.

    The Connection is supplied by the caller — the repo doesn't
    manage transactions. Callers wrap individual operations
    (`upsert`, `delete_by_exchange`) in their own `db_engine.begin()`
    blocks; reads typically use `db_engine.connect()`.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- reads ---------- #

    def get(
        self,
        *,
        exchange: str,
        ticker_id: int,
        timeframe: Timeframe,
    ) -> SignalBacktestRun | None:
        """Return the resume row for one (exchange, ticker, timeframe),
        or None if it doesn't exist (ticker has never been computed
        for this exchange+timeframe pair)."""
        row = self._conn.execute(
            text("""
                SELECT exchange, ticker_id, timeframe,
                       computed_from_date, computed_through_date,
                       computed_at, last_run_id
                FROM signal_backtest_runs
                WHERE exchange = :ex
                  AND ticker_id = :tid
                  AND timeframe = :tf
            """),
            {"ex": exchange, "tid": ticker_id, "tf": timeframe.value},
        ).first()
        if row is None:
            return None
        return _row_to_record(row)

    def get_for_exchange(self, exchange: str) -> list[SignalBacktestRun]:
        """Return every resume row for `exchange`. Ordered by
        (ticker_id, timeframe) for deterministic test output."""
        rows = self._conn.execute(
            text("""
                SELECT exchange, ticker_id, timeframe,
                       computed_from_date, computed_through_date,
                       computed_at, last_run_id
                FROM signal_backtest_runs
                WHERE exchange = :ex
                ORDER BY ticker_id ASC, timeframe ASC
            """),
            {"ex": exchange},
        )
        return [_row_to_record(r) for r in rows]

    # ---------- writes ---------- #

    def upsert(
        self,
        *,
        exchange: str,
        ticker_id: int,
        timeframe: Timeframe,
        computed_from_date: date,
        computed_through_date: date,
        last_run_id: int | None = None,
    ) -> None:
        """Insert or update the resume row.

        `computed_from_date` semantics on update:
          - If the row already exists, we COALESCE to keep the
            ORIGINAL computed_from_date. The window's left edge is
            the earliest date ever covered; subsequent incremental
            runs extend the right edge but never push the left edge
            forward.
          - If the row doesn't exist, this is the first time we've
            computed for (ticker, timeframe) and the passed-in value
            wins.

        `computed_through_date` is always the new value — that's the
        whole point of an incremental update.

        `computed_at` is bumped to NOW() on every call so the GUI's
        "what's stale" view has a useful field.
        """
        self._conn.execute(
            text("""
                INSERT INTO signal_backtest_runs (
                    exchange, ticker_id, timeframe,
                    computed_from_date, computed_through_date,
                    computed_at, last_run_id
                ) VALUES (
                    :ex, :tid, :tf,
                    :from_date, :through_date,
                    NOW(), :run_id
                )
                ON CONFLICT (exchange, ticker_id, timeframe)
                DO UPDATE SET
                    computed_from_date  = signal_backtest_runs.computed_from_date,
                    computed_through_date = EXCLUDED.computed_through_date,
                    computed_at         = NOW(),
                    last_run_id         = EXCLUDED.last_run_id
            """),
            {
                "ex": exchange,
                "tid": ticker_id,
                "tf": timeframe.value,
                "from_date": computed_from_date,
                "through_date": computed_through_date,
                "run_id": last_run_id,
            },
        )

    def delete_by_exchange(self, exchange: str) -> int:
        """Wipe all resume rows for `exchange`. Used by the
        `force_full=true` operator workflow in a small dedicated
        transaction at run start. Returns the number of rows deleted.
        """
        result = self._conn.execute(
            text("""
                DELETE FROM signal_backtest_runs
                WHERE exchange = :ex
            """),
            {"ex": exchange},
        )
        return result.rowcount or 0
