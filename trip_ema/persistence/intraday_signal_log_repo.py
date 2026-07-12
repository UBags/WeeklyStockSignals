"""Repository for `intraday_signal_log` — the within-day signal chain."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

SIGNAL_TYPES = ("BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL")


@dataclass(frozen=True)
class IntradayTransition:
    ticker_id: int
    timeframe: str
    signal_date: date
    bar_timestamp: datetime
    signal_type: str
    regime_before: int
    regime_after: int
    price_at_signal: float
    computed_at: datetime


class IntradaySignalLogRepository:
    """Append-only. Several rows per (ticker, signal_date) by design."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    # ---------- writes ---------- #

    def append(self, t: IntradayTransition) -> None:
        if t.signal_type not in SIGNAL_TYPES:
            raise ValueError(f"unknown signal_type {t.signal_type!r}")
        if t.regime_before == t.regime_after:
            raise ValueError(
                "regime_before == regime_after; not a transition"
            )
        if t.price_at_signal <= 0:
            raise ValueError("price_at_signal must be > 0")
        self._conn.execute(
            text("""
                INSERT INTO intraday_signal_log (
                    ticker_id, timeframe, signal_date, bar_timestamp,
                    signal_type, regime_before, regime_after,
                    price_at_signal, computed_at
                ) VALUES (
                    :ticker_id, :timeframe, :signal_date, :bar_timestamp,
                    :signal_type, :regime_before, :regime_after,
                    :price_at_signal, :computed_at
                )
            """),
            {
                "ticker_id": t.ticker_id,
                "timeframe": t.timeframe,
                "signal_date": t.signal_date,
                "bar_timestamp": t.bar_timestamp,
                "signal_type": t.signal_type,
                "regime_before": t.regime_before,
                "regime_after": t.regime_after,
                "price_at_signal": t.price_at_signal,
                "computed_at": t.computed_at,
            },
        )

    def delete_for_date(self, *, signal_date: date) -> int:
        """Clear a day's chain. Lets a re-run of the day self-correct."""
        return self._conn.execute(
            text("DELETE FROM intraday_signal_log WHERE signal_date = :d"),
            {"d": signal_date},
        ).rowcount

    # ---------- reads ---------- #

    def latest_regime_after(
        self, ticker_id: int, timeframe: str, signal_date: date,
    ) -> int | None:
        """regime_after of the most recent transition logged for this
        (ticker, day), or None when the chain is empty.

        This is the ticker's current *position* within the day — the baseline
        the next tick is compared against.
        """
        row = self._conn.execute(
            text("""
                SELECT regime_after
                  FROM intraday_signal_log
                 WHERE ticker_id = :tid AND timeframe = :tf
                   AND signal_date = :d
                 ORDER BY computed_at DESC, log_id DESC
                 LIMIT 1
            """),
            {"tid": ticker_id, "tf": timeframe, "d": signal_date},
        ).first()
        return int(row[0]) if row is not None else None

    def chains_for_date(
        self, *, signal_date: date, timeframe: str = "1W",
        ticker_ids: list[int] | None = None,
    ) -> dict[int, list[str]]:
        """{ticker_id: [signal_type, ...]} in chronological order, for one day.

        The email renders this as e.g. "BUY - EXIT_FROM_BUY - BUY".
        """
        sql = """
            SELECT ticker_id, signal_type
              FROM intraday_signal_log
             WHERE signal_date = :d AND timeframe = :tf
        """
        params: dict = {"d": signal_date, "tf": timeframe}
        if ticker_ids is not None:
            if not ticker_ids:
                return {}
            sql += " AND ticker_id = ANY(:ids)"
            params["ids"] = list(ticker_ids)
        sql += " ORDER BY ticker_id, computed_at ASC, log_id ASC"

        out: dict[int, list[str]] = {}
        for r in self._conn.execute(text(sql), params):
            out.setdefault(int(r.ticker_id), []).append(r.signal_type)
        return out
