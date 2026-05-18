"""OHLCV bars repository.

Hot read path: `fetch_history(ticker_id, timeframe, limit)` for engine
priming, and `fetch_since(ticker_id, timeframe, since)` for incremental
ingest in the scheduler.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, insert, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..domain import OHLCVBar, Timeframe
from .tables import ohlcv_bars


def _row_to_bar(row) -> OHLCVBar:
    return OHLCVBar(
        timestamp=row.bar_timestamp,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
    )


class BarsRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    def fetch_history(
        self,
        ticker_id: int,
        timeframe: Timeframe,
        *,
        limit: int = 1000,
    ) -> list[OHLCVBar]:
        """Return the most recent `limit` bars for a ticker+timeframe,
        ordered OLDEST FIRST (chronological order — the order TickerEngine
        expects for priming)."""
        stmt = (
            select(ohlcv_bars)
            .where(
                ohlcv_bars.c.ticker_id == ticker_id,
                ohlcv_bars.c.timeframe == timeframe.value,
            )
            .order_by(ohlcv_bars.c.bar_timestamp.desc())
            .limit(limit)
        )
        rows = list(self._conn.execute(stmt))
        # We fetched desc to use the index; flip back to chronological.
        return [_row_to_bar(r) for r in reversed(rows)]

    def fetch_since(
        self,
        ticker_id: int,
        timeframe: Timeframe,
        since_exclusive: datetime,
    ) -> list[OHLCVBar]:
        """Return bars with timestamp STRICTLY GREATER than `since_exclusive`.

        Used by the ComputeScheduler: 'give me everything newer than what
        my engine has already ingested'. Ordered chronologically."""
        stmt = (
            select(ohlcv_bars)
            .where(
                ohlcv_bars.c.ticker_id == ticker_id,
                ohlcv_bars.c.timeframe == timeframe.value,
                ohlcv_bars.c.bar_timestamp > since_exclusive,
            )
            .order_by(ohlcv_bars.c.bar_timestamp.asc())
        )
        return [_row_to_bar(r) for r in self._conn.execute(stmt)]

    def latest_timestamp(
        self,
        ticker_id: int,
        timeframe: Timeframe,
    ) -> datetime | None:
        """Convenience: timestamp of the newest bar, or None if no bars exist."""
        stmt = (
            select(ohlcv_bars.c.bar_timestamp)
            .where(
                ohlcv_bars.c.ticker_id == ticker_id,
                ohlcv_bars.c.timeframe == timeframe.value,
            )
            .order_by(ohlcv_bars.c.bar_timestamp.desc())
            .limit(1)
        )
        row = self._conn.execute(stmt).first()
        return row[0] if row else None

    def max_updated_at_since(
        self,
        ticker_id: int,
        timeframe: Timeframe,
        history_start_inclusive: datetime,
    ) -> datetime | None:
        """Return the latest `updated_at` for any bar whose `bar_timestamp`
        is greater than or equal to `history_start_inclusive`.

        Used by the ComputeScheduler to detect historical revisions: if
        the ETL rewrote a bar from inside the engine's history window
        (e.g., after a stock split or exchange correction), `updated_at`
        for that row will move forward. Comparing this value to a
        previously-recorded high-water mark tells the scheduler whether
        to force a full re-prime.

        A4 fix: this used strict `>` previously and excluded the oldest
        bar in the engine's history window. A correction to the
        very-first bar would silently slip past the revision check. The
        bound is now inclusive on both ends — the oldest bar is part of
        the window we care about.

        Returns None if no bars match (e.g., empty history).
        """
        stmt = (
            select(func.max(ohlcv_bars.c.updated_at))
            .where(
                ohlcv_bars.c.ticker_id == ticker_id,
                ohlcv_bars.c.timeframe == timeframe.value,
                ohlcv_bars.c.bar_timestamp >= history_start_inclusive,
            )
        )
        return self._conn.execute(stmt).scalar()

    # ---------- Writes (primarily for tests / ETL) ---------- #

    def insert_bar(
        self,
        ticker_id: int,
        timeframe: Timeframe,
        bar: OHLCVBar,
    ) -> None:
        """Insert one bar. Used by tests and seed scripts. The production
        path expects an external ETL to be the writer, so this isn't on the
        hot path."""
        stmt = insert(ohlcv_bars).values(
            ticker_id=ticker_id,
            timeframe=timeframe.value,
            bar_timestamp=bar.timestamp,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        self._conn.execute(stmt)

    def upsert_bar(
        self,
        ticker_id: int,
        timeframe: Timeframe,
        bar: OHLCVBar,
    ) -> None:
        """Insert-or-update. Useful when the ETL re-emits bars (e.g. when
        the latest bar gets corrected)."""
        stmt = pg_insert(ohlcv_bars).values(
            ticker_id=ticker_id,
            timeframe=timeframe.value,
            bar_timestamp=bar.timestamp,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["ticker_id", "timeframe", "bar_timestamp"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        self._conn.execute(stmt)

    def weekly_buckets_with_bars(
        self,
        ticker_id: int,
        timeframe: Timeframe,
        since_inclusive: datetime,
        tz: str,
    ) -> set[datetime]:
        """Return the set of UTC Monday timestamps for which at least
        one bar exists in this ticker's timeframe at or after
        `since_inclusive`. Anchored to the supplied `tz` (so a NSE
        ticker uses 'Asia/Kolkata', an NYSE ticker uses
        'America/New_York').

        This is the server-side fast path for gap detection. Equivalent
        Python:

            bars = self.fetch_since(ticker_id, timeframe, since)
            return {_monday_of(b.timestamp, tz=tz) for b in bars}

        but executes as ONE SQL with a GROUP BY rather than transferring
        every bar in the window. For a hourly source over a multi-week
        gap that's hundreds of rows vs. a handful.

        DST correctness: `date_trunc('week', local)` floors a naive
        local timestamp to Monday-00:00 local. Wrapping the result in
        `AT TIME ZONE :tz` re-interprets that local-Monday-midnight as
        a `timestamptz`, applying the correct UTC offset for that
        specific date (including DST transitions). Matches the Python
        `_monday_of` semantics exactly.
        """
        # We use a raw SQL fragment because the bind-double-use of `:tz`
        # for the AT TIME ZONE on both timestamptz→naive and naive→
        # timestamptz directions is cleanest as text.
        sql = text("""
            SELECT
                (
                    date_trunc('week', bar_timestamp AT TIME ZONE :tz)
                        AT TIME ZONE :tz
                ) AS local_week_utc
            FROM ohlcv_bars
            WHERE ticker_id = :ticker_id
              AND timeframe = :tf
              AND bar_timestamp >= :since_inclusive
            GROUP BY local_week_utc
        """)
        result = self._conn.execute(
            sql,
            {
                "tz": tz,
                "ticker_id": ticker_id,
                "tf": timeframe.value,
                "since_inclusive": since_inclusive,
            },
        )
        return {row.local_week_utc for row in result}
