"""Repository for fundamentals_fetch_status.

What we asked the fundamentals provider, and what it said. See migration
0049_fundamentals_fetch_status for why this lives beside instrument_shares and
quarterly_financials rather than inside them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

DATASETS = ("shares", "financials", "industry")
STATUS_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class FetchStatus:
    ticker_id: int
    dataset: str
    status: str
    reason: str | None
    first_seen_at: datetime
    last_checked_at: datetime
    attempts: int


class FundamentalsFetchStatusRepository:
    """Records provider-side absence, so we stop re-asking a dead question and
    so the fidelity audit can tell "missing" from "does not exist"."""

    def __init__(self, conn: Connection):
        self._conn = conn

    @staticmethod
    def _check_dataset(dataset: str) -> None:
        if dataset not in DATASETS:
            raise ValueError(
                f"dataset must be one of {DATASETS}, got {dataset!r}"
            )

    def mark_unavailable(
        self, ticker_id: int, dataset: str, reason: str | None = None,
    ) -> None:
        """The provider was asked and has nothing.

        Idempotent per (ticker_id, dataset). Re-marking bumps `attempts` and
        `last_checked_at` but PRESERVES `first_seen_at` — that is the field that
        says how long a ticker has been dark, and it must survive the monthly
        re-check that re-asks and re-marks.
        """
        self._check_dataset(dataset)
        self._conn.execute(
            text("""
                INSERT INTO fundamentals_fetch_status
                    (ticker_id, dataset, status, reason,
                     first_seen_at, last_checked_at, attempts)
                VALUES (:tid, :ds, 'UNAVAILABLE', :reason, NOW(), NOW(), 1)
                ON CONFLICT (ticker_id, dataset) DO UPDATE SET
                    status          = 'UNAVAILABLE',
                    reason          = EXCLUDED.reason,
                    last_checked_at = NOW(),
                    attempts        = fundamentals_fetch_status.attempts + 1
            """),
            {"tid": ticker_id, "ds": dataset, "reason": reason},
        )

    def clear(self, ticker_id: int, dataset: str) -> bool:
        """The provider now HAS data for this ticker. Drop the verdict.

        Called when a fetch succeeds for a ticker previously marked dark — a
        microcap picking up yfinance coverage. Without this the record would
        outlive its truth.
        """
        self._check_dataset(dataset)
        r = self._conn.execute(
            text("""
                DELETE FROM fundamentals_fetch_status
                 WHERE ticker_id = :tid AND dataset = :ds
            """),
            {"tid": ticker_id, "ds": dataset},
        )
        return r.rowcount > 0

    def list_unavailable(
        self, dataset: str, *, exchange: str | None = None,
    ) -> set[int]:
        """ticker_ids known-dark for `dataset`. The gap-only sweep subtracts
        these, which is what turns 107 doomed fetches per run into 0."""
        self._check_dataset(dataset)
        sql = """
            SELECT f.ticker_id
              FROM fundamentals_fetch_status f
             WHERE f.dataset = :ds AND f.status = 'UNAVAILABLE'
        """
        params: dict = {"ds": dataset}
        if exchange is not None:
            sql += """
              AND EXISTS (
                  SELECT 1 FROM tickers t
                   WHERE t.ticker_id = f.ticker_id AND t.exchange = :ex
              )"""
            params["ex"] = exchange
        rows = self._conn.execute(text(sql), params)
        return {int(r[0]) for r in rows}

    def counts_by_dataset(self, exchange: str) -> dict[str, int]:
        """{dataset: n_unavailable} for one exchange. The audit's third bucket."""
        rows = self._conn.execute(
            text("""
                SELECT f.dataset, count(*)
                  FROM fundamentals_fetch_status f
                  JOIN tickers t ON t.ticker_id = f.ticker_id
                 WHERE t.exchange = :ex
                   AND t.is_active AND t.delisted_at IS NULL
                   AND f.status = 'UNAVAILABLE'
                 GROUP BY f.dataset
            """),
            {"ex": exchange},
        )
        return {r[0]: int(r[1]) for r in rows}

    def get(self, ticker_id: int, dataset: str) -> FetchStatus | None:
        self._check_dataset(dataset)
        row = self._conn.execute(
            text("""
                SELECT ticker_id, dataset, status, reason,
                       first_seen_at, last_checked_at, attempts
                  FROM fundamentals_fetch_status
                 WHERE ticker_id = :tid AND dataset = :ds
            """),
            {"tid": ticker_id, "ds": dataset},
        ).fetchone()
        if row is None:
            return None
        return FetchStatus(
            ticker_id=int(row[0]), dataset=row[1], status=row[2],
            reason=row[3], first_seen_at=row[4], last_checked_at=row[5],
            attempts=int(row[6]),
        )
