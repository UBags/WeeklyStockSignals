"""Tests for CurrentSignalsRepository.delete_for_tickers.

Step 11b1 added this method to support backfill's Stage 9 — when
the latest-bar derivation shows no flip for a ticker, any stale
current_signals row must be removed.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from trip_ema.domain import Timeframe
from trip_ema.domain.signal_event import SignalEvent
from trip_ema.persistence import transaction
from trip_ema.persistence.current_signals_repo import (
    CurrentSignalsRepository,
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


def _insert_current_signal(db_engine, ticker_id: int) -> None:
    ev = SignalEvent(
        ticker_id=ticker_id,
        timeframe=str(Timeframe.WEEKLY),
        bar_timestamp=datetime(2024, 6, 3, tzinfo=UTC),
        signal_type="BUY",
        regime_before=-1,
        regime_after=1,
        price_at_signal=100.0,
        computed_at=datetime(2024, 6, 3, tzinfo=UTC),
    )
    with transaction(db_engine) as conn:
        CurrentSignalsRepository(conn).insert_many([ev])


class TestDeleteForTickers:

    def test_empty_list_no_op(self, db_engine):
        with transaction(db_engine) as conn:
            n = CurrentSignalsRepository(conn).delete_for_tickers([])
        assert n == 0

    def test_deletes_listed_tickers(self, db_engine):
        for tid in (1, 2, 3):
            _seed_ticker(db_engine, tid, f"T{tid}")
            _insert_current_signal(db_engine, tid)

        with transaction(db_engine) as conn:
            n = CurrentSignalsRepository(conn).delete_for_tickers(
                [1, 3],
            )
        assert n == 2

        # Only ticker 2 should remain
        with db_engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT ticker_id FROM current_signals"
                " ORDER BY ticker_id"
            )).all()
        assert [r.ticker_id for r in rows] == [2]

    def test_missing_tickers_silently_ignored(self, db_engine):
        """Asking to delete a ticker_id that doesn't have a row is
        fine — the delete just affects 0 rows for that id."""
        _seed_ticker(db_engine, 1, "T1")
        _insert_current_signal(db_engine, 1)

        with transaction(db_engine) as conn:
            n = CurrentSignalsRepository(conn).delete_for_tickers(
                [1, 999, 1000],
            )
        assert n == 1   # only ticker 1 actually had a row

    def test_returns_zero_when_no_match(self, db_engine):
        _seed_ticker(db_engine, 1, "T1")
        # No current_signals row inserted
        with transaction(db_engine) as conn:
            n = CurrentSignalsRepository(conn).delete_for_tickers(
                [1],
            )
        assert n == 0
