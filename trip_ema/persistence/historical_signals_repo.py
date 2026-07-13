"""Repository for historical_signals — V1 append-only audit log.

Every signal ever fired, in any cycle. No uniqueness beyond
`signal_id BIGSERIAL` — rotation across cycles intentionally
produces duplicate (ticker_id, bar_timestamp, signal_type) rows
while the source bar remains the latest. Read-side
`DISTINCT ON (bar_timestamp, signal_type)` dedupes for GUI display.

App-level validation matches CurrentSignalsRepository (same DB CHECK
constraints).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.signal_event import (
    REGIME_VALUES,
    SIGNAL_TYPES,
    SignalEvent,
)
from ..domain.market_session import (
    exchange_of_conn,
    trading_date_of,
)


# Column order — must match _row_to_signal unpacking. We do NOT
# select `archived_at` — it's persistence-internal audit data.
# Callers that need it query directly.
_SELECT_COLS = (
    "ticker_id, timeframe, bar_timestamp, signal_type, "
    "regime_before, regime_after, price_at_signal, computed_at"
)


_INSERT_SQL = """
    INSERT INTO historical_signals (
        ticker_id, timeframe, bar_timestamp, signal_type,
        regime_before, regime_after, price_at_signal,
        computed_at, archived_at, signal_date
    ) VALUES (
        :tid, :tf, :bt, :st,
        :rb, :ra, :p, :ca, COALESCE(:aa, NOW()), :sd
    )
"""


class HistoricalSignalsRepository:
    """V1 historical_signals CRUD.

    Two writers: (1) the rotation primitive (Step 4) which moves
    rows from current_signals on each cycle, and (2) the backfill
    pipeline (Step 11b1) which uses delete_for_tickers_from_date +
    insert_many to rewrite a date range when an operator runs a
    historical replay.
    """

    def __init__(self, conn: Connection):
        self._conn = conn
        self._exchange_cache: str | None = None

    # ---------- writes ---------- #

    def delete_for_tickers_from_date(
        self,
        *,
        ticker_ids: list[int],
        from_date: datetime,
        to_date: datetime | None = None,
    ) -> int:
        """Delete historical_signals rows for the given ticker_ids
        where computed_at >= from_date (and <= to_date when given).

        Step 11b1 (backfill): breaks the original "pure append-only"
        invariant — backfill needs to wipe rows in a historical range
        before re-inserting fresh ones. The rotation primitive (Step 4)
        still treats historical as append-only.

        DELETE-UPPER-BOUND: to_date bounds the delete to the walk's upper
        edge. The daywalk only regenerates [date_from, date_to], so an
        unbounded delete on a bounded run would leave a hole of missing
        signals in (date_to, today]. to_date=None preserves the original
        unbounded behaviour for non-backfill callers.

        Returns the row count deleted.
        """
        if not ticker_ids:
            return 0
        sql = (
            "DELETE FROM historical_signals "
            "WHERE ticker_id = ANY(:ids) "
            "AND computed_at >= :from_dt"
        )
        params: dict = {"ids": ticker_ids, "from_dt": from_date}
        if to_date is not None:
            # Half-open. `<=` forced the caller to invent a last representable
            # instant (23:59:59.999999), which is a fact about microsecond storage
            # precision, not about the end of a day. Callers now pass the next
            # local midnight; the boundary belongs to the next day and to nobody
            # else.
            sql += " AND computed_at < :to_dt"
            params["to_dt"] = to_date
        result = self._conn.execute(text(sql), params)
        return result.rowcount

    def delete_for_tickers_on_date(
        self,
        *,
        ticker_ids: list[int],
        signal_date,          # datetime.date; annotation-only under future-annotations
    ) -> int:
        """Delete this ticker-set's rows for exactly `signal_date`.

        The idempotent primitive for the per-day model: 'process day D'
        deletes D's rows then re-inserts, so a re-run self-corrects
        rather than duplicating.
        """
        if not ticker_ids:
            return 0
        result = self._conn.execute(
            text("""
                DELETE FROM historical_signals
                 WHERE ticker_id = ANY(:ids)
                   AND signal_date = :d
            """),
            {"ids": ticker_ids, "d": signal_date},
        )
        return result.rowcount

    def insert_many(
        self,
        signals: list[SignalEvent],
        *,
        archived_at: datetime | None = None,
    ) -> int:
        """Append N signals in one round-trip. Validates each first.

        Args:
            signals: rows to append.
            archived_at: when the signals are being archived. If None,
                uses the DB default (NOW()). The rotation primitive
                will pass a single timestamp so all rows in one
                rotation batch share the same archived_at.

        Returns:
            count written (== len(signals)).
        """
        if not signals:
            return 0
        for s in signals:
            self._validate(s)
        self._conn.execute(
            text(_INSERT_SQL),
            [self._params(s, archived_at) for s in signals],
        )
        return len(signals)

    # ---------- reads ---------- #

    def get_for_ticker(
        self,
        ticker_id: int,
        *,
        kinds: list[str] | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int | None = None,
    ) -> list[SignalEvent]:
        """Historical signals for one ticker, newest-first by
        (bar_timestamp, computed_at).

        The composite ORDER BY is intentional: when the same bar
        is re-detected as a regime change across multiple cycles
        (same bar_timestamp, different computed_at per cycle),
        the most recently computed copy sorts first.

        Args:
            ticker_id: required.
            kinds: optional filter (subset of SIGNAL_TYPES).
                None means no filter.
            from_date: optional lower bound on signal_date (inclusive).
            to_date: optional upper bound on signal_date (inclusive).

        SIGNAL-DATE-BOUND: these bound `signal_date` (the daily grain, and the
        column carrying the UNIQUE constraint), NOT bar_timestamp.
        bar_timestamp is the IST-Monday week anchor shared by every day of the
        week, so filtering on it matches by WEEK: a Thursday signal would match
        a filter on that week's Monday and miss one on the Thursday itself.
        Harmless while every signal landed on the week's last day; wrong now
        that signals are per-day.
            limit: optional row cap.
        """
        conditions = ["ticker_id = :tid"]
        params: dict = {"tid": ticker_id}

        if kinds:
            for k in kinds:
                if k not in SIGNAL_TYPES:
                    raise ValueError(
                        f"unknown signal_type {k!r}; "
                        f"must be in {SIGNAL_TYPES}"
                    )
            placeholders = ", ".join(
                f":k{i}" for i in range(len(kinds))
            )
            conditions.append(f"signal_type IN ({placeholders})")
            for i, k in enumerate(kinds):
                params[f"k{i}"] = k

        if from_date is not None:
            # SIGNAL-DATE-BOUND: daily grain, not the week anchor.
            conditions.append("signal_date >= :fd")
            params["fd"] = from_date
        if to_date is not None:
            conditions.append("signal_date <= :td")
            params["td"] = to_date

        sql_parts = [
            f"SELECT {_SELECT_COLS}",
            "FROM historical_signals",
            "WHERE " + " AND ".join(conditions),
            "ORDER BY bar_timestamp DESC, computed_at DESC",
        ]
        if limit is not None:
            sql_parts.append("LIMIT :lim")
            params["lim"] = limit

        rows = self._conn.execute(text("\n".join(sql_parts)), params)
        return [_row_to_signal(r) for r in rows]

    def count_for_ticker(self, ticker_id: int) -> int:
        """Total historical rows for one ticker."""
        return self._conn.execute(
            text("""
                SELECT COUNT(*) FROM historical_signals
                WHERE ticker_id = :tid
            """),
            {"tid": ticker_id},
        ).scalar_one()

    def count(self) -> int:
        """Total historical rows across all tickers. Used by smoke
        tests and admin status."""
        return self._conn.execute(
            text("SELECT COUNT(*) FROM historical_signals"),
        ).scalar_one()

    # ---------- internals ---------- #

    @staticmethod
    def _validate(s: SignalEvent) -> None:
        if s.signal_type not in SIGNAL_TYPES:
            raise ValueError(
                f"signal_type must be in {SIGNAL_TYPES}, "
                f"got {s.signal_type!r}"
            )
        if s.regime_before not in REGIME_VALUES:
            raise ValueError(
                f"regime_before must be in {REGIME_VALUES}, "
                f"got {s.regime_before}"
            )
        if s.regime_after not in REGIME_VALUES:
            raise ValueError(
                f"regime_after must be in {REGIME_VALUES}, "
                f"got {s.regime_after}"
            )
        if s.regime_before == s.regime_after:
            raise ValueError(
                f"regime_before == regime_after ({s.regime_before}) "
                f"— a signal is a regime CHANGE by definition"
            )
        if not (s.price_at_signal > 0):
            raise ValueError(
                f"price_at_signal must be > 0, "
                f"got {s.price_at_signal}"
            )

    def _params(self, s: SignalEvent, archived_at: datetime | None) -> dict:
        return {
            "tid": s.ticker_id,
            "tf": s.timeframe,
            "bt": s.bar_timestamp,
            "st": s.signal_type,
            "rb": s.regime_before,
            "ra": s.regime_after,
            "p": s.price_at_signal,
            "ca": s.computed_at,
            "aa": archived_at,   # None → DB default NOW()
            # NOT s.bar_timestamp.date(). bar_timestamp is the week anchor — the
            # local-Monday-midnight INSTANT, which is Sunday 18:30 UTC for NSE. A
            # bare .date() on it answers "Sunday" the moment the process is not
            # running on an Indian clock, and signal_date is what the per-day
            # delete/insert primitive keys on.
            "sd": (s.signal_date if s.signal_date is not None
                   else trading_date_of(s.bar_timestamp, self._exchange())),
        }

    def _exchange(self) -> str:
        """Resolved once per repository instance — the value cannot change
        underneath a connection, and a per-row query would be absurd."""
        if self._exchange_cache is None:
            self._exchange_cache = exchange_of_conn(self._conn)
        return self._exchange_cache


def _row_to_signal(row) -> SignalEvent:
    """Unpack one SELECT row into a SignalEvent. Column order
    matches `_SELECT_COLS`."""
    return SignalEvent(
        ticker_id=int(row[0]),
        timeframe=row[1],
        bar_timestamp=row[2],
        signal_type=row[3],
        regime_before=int(row[4]),
        regime_after=int(row[5]),
        price_at_signal=float(row[6]),
        computed_at=row[7],
    )
