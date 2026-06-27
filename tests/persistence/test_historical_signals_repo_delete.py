"""Tests for HistoricalSignalsRepository.delete_for_tickers_from_date.

Step 11b1 added this method to support backfill's Stage 7 — wipe a
date range for scoped tickers before re-inserting fresh rows.
Breaks the original "pure append-only" invariant; that's by design.
"""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import text

from trip_ema.domain import Timeframe
from trip_ema.domain.signal_event import SignalEvent
from trip_ema.persistence import transaction
from trip_ema.persistence.historical_signals_repo import (
    HistoricalSignalsRepository,
)


def _insert_signal(
    db_engine, *, ticker_id: int,
    bar_ts: datetime, computed_at: datetime,
    archived_at: datetime | None = None,
) -> None:
    ev = SignalEvent(
        ticker_id=ticker_id,
        timeframe=str(Timeframe.WEEKLY),
        bar_timestamp=bar_ts,
        signal_type="BUY",
        regime_before=-1,
        regime_after=1,
        price_at_signal=100.0,
        computed_at=computed_at,
    )
    with transaction(db_engine) as conn:
        HistoricalSignalsRepository(conn).insert_many(
            [ev], archived_at=archived_at or datetime.now(UTC),
        )


def _seed_ticker(db_engine, ticker_id: int, symbol: str) -> None:
    with transaction(db_engine) as conn:
        conn.execute(
            text("""
                INSERT INTO tickers (
                    ticker_id, exchange, symbol, tier,
                    is_active, delisted_at
                ) VALUES (:tid, 'NSE', :sym, 2, TRUE, NULL)
                ON CONFLICT (ticker_id) DO NOTHING
            """),
            {"tid": ticker_id, "sym": symbol},
        )


class TestDeleteForTickersFromDate:

    def test_empty_ticker_ids_no_op(self, db_engine):
        with transaction(db_engine) as conn:
            n = HistoricalSignalsRepository(
                conn,
            ).delete_for_tickers_from_date(
                ticker_ids=[],
                from_date=datetime(2024, 1, 1, tzinfo=UTC),
            )
        assert n == 0

    def test_deletes_only_matching_rows(self, db_engine):
        _seed_ticker(db_engine, 1, "T1")
        _seed_ticker(db_engine, 2, "T2")
        # Insert: ticker 1 with 3 signals across May+June; ticker 2
        # with one signal in May.
        _insert_signal(
            db_engine, ticker_id=1,
            bar_ts=datetime(2024, 5, 6, tzinfo=UTC),
            computed_at=datetime(2024, 5, 6, tzinfo=UTC),
        )
        _insert_signal(
            db_engine, ticker_id=1,
            bar_ts=datetime(2024, 6, 3, tzinfo=UTC),
            computed_at=datetime(2024, 6, 3, tzinfo=UTC),
        )
        _insert_signal(
            db_engine, ticker_id=1,
            bar_ts=datetime(2024, 6, 10, tzinfo=UTC),
            computed_at=datetime(2024, 6, 10, tzinfo=UTC),
        )
        _insert_signal(
            db_engine, ticker_id=2,
            bar_ts=datetime(2024, 5, 6, tzinfo=UTC),
            computed_at=datetime(2024, 5, 6, tzinfo=UTC),
        )

        # Delete ticker 1 rows from June 1 onwards
        with transaction(db_engine) as conn:
            n = HistoricalSignalsRepository(
                conn,
            ).delete_for_tickers_from_date(
                ticker_ids=[1],
                from_date=datetime(2024, 6, 1, tzinfo=UTC),
            )
        assert n == 2

        # Ticker 1 May row + ticker 2 May row should remain
        with db_engine.connect() as conn:
            count = conn.execute(text(
                "SELECT COUNT(*) FROM historical_signals"
            )).scalar_one()
        assert count == 2

    def test_does_not_touch_other_ticker_rows(self, db_engine):
        _seed_ticker(db_engine, 1, "T1")
        _seed_ticker(db_engine, 2, "T2")
        for tid in (1, 2):
            _insert_signal(
                db_engine, ticker_id=tid,
                bar_ts=datetime(2024, 6, 3, tzinfo=UTC),
                computed_at=datetime(2024, 6, 3, tzinfo=UTC),
            )
        with transaction(db_engine) as conn:
            n = HistoricalSignalsRepository(
                conn,
            ).delete_for_tickers_from_date(
                ticker_ids=[1],
                from_date=datetime(2024, 1, 1, tzinfo=UTC),
            )
        assert n == 1
        # Ticker 2's row survives
        with db_engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT ticker_id FROM historical_signals"
            )).all()
        assert [r.ticker_id for r in rows] == [2]

    def test_from_date_is_inclusive(self, db_engine):
        """from_date semantics: computed_at >= from_date (inclusive)."""
        _seed_ticker(db_engine, 1, "T1")
        cutoff = datetime(2024, 6, 10, 12, 0, tzinfo=UTC)
        # Row exactly at cutoff is INCLUDED in delete
        _insert_signal(
            db_engine, ticker_id=1,
            bar_ts=cutoff,
            computed_at=cutoff,
        )
        # Row just before cutoff is NOT
        _insert_signal(
            db_engine, ticker_id=1,
            bar_ts=cutoff - timedelta(seconds=1),
            computed_at=cutoff - timedelta(seconds=1),
        )
        with transaction(db_engine) as conn:
            n = HistoricalSignalsRepository(
                conn,
            ).delete_for_tickers_from_date(
                ticker_ids=[1], from_date=cutoff,
            )
        assert n == 1
        # The pre-cutoff row remains
        with db_engine.connect() as conn:
            count = conn.execute(text(
                "SELECT COUNT(*) FROM historical_signals"
            )).scalar_one()
        assert count == 1
