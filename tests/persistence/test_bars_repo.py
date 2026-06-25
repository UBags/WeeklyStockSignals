"""Tests for BarsRepository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import pytest

from trip_ema.domain import OHLCVBar, TickerRef, Timeframe
from trip_ema.persistence import BarsRepository, TickersRepository, transaction


@pytest.fixture
def ticker_id(db_engine):
    """Helper: create a ticker once per test, return its id."""
    with transaction(db_engine) as conn:
        return TickersRepository(conn).insert(
            TickerRef("NSE", "NIFTY", "INDEX"),
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )


def _bar(day: int, close: float = 100.0) -> OHLCVBar:
    return OHLCVBar(
        timestamp=datetime(2024, 1, day, tzinfo=UTC),
        open=close - 0.5, high=close + 1, low=close - 1, close=close,
        volume=1000.0,
    )


def test_insert_and_fetch_one_bar(db_engine, ticker_id):
    bar = _bar(1, close=100.0)
    with transaction(db_engine) as conn:
        BarsRepository(conn).insert_bar(ticker_id, Timeframe.DAILY, bar)
    with db_engine.connect() as conn:
        bars = BarsRepository(conn).fetch_history(ticker_id, Timeframe.DAILY)
    assert len(bars) == 1
    assert bars[0].close == 100.0
    assert bars[0].timestamp == bar.timestamp


def test_fetch_history_returns_chronological_order(db_engine, ticker_id):
    with transaction(db_engine) as conn:
        repo = BarsRepository(conn)
        for i in [3, 1, 5, 2, 4]:   # insert out of order
            repo.insert_bar(ticker_id, Timeframe.DAILY, _bar(i, close=100 + i))
    with db_engine.connect() as conn:
        bars = BarsRepository(conn).fetch_history(ticker_id, Timeframe.DAILY)
    days = [b.timestamp.day for b in bars]
    assert days == [1, 2, 3, 4, 5]


def test_fetch_history_respects_limit(db_engine, ticker_id):
    with transaction(db_engine) as conn:
        repo = BarsRepository(conn)
        for i in range(1, 11):
            repo.insert_bar(ticker_id, Timeframe.DAILY, _bar(i))
    with db_engine.connect() as conn:
        bars = BarsRepository(conn).fetch_history(ticker_id, Timeframe.DAILY, limit=3)
    assert len(bars) == 3
    # The 3 most recent, in chronological order
    assert [b.timestamp.day for b in bars] == [8, 9, 10]


def test_fetch_since_returns_only_newer_bars(db_engine, ticker_id):
    with transaction(db_engine) as conn:
        repo = BarsRepository(conn)
        for i in range(1, 6):
            repo.insert_bar(ticker_id, Timeframe.DAILY, _bar(i))
    cutoff = datetime(2024, 1, 3, tzinfo=UTC)
    with db_engine.connect() as conn:
        bars = BarsRepository(conn).fetch_since(ticker_id, Timeframe.DAILY, cutoff)
    assert [b.timestamp.day for b in bars] == [4, 5]


def test_fetch_since_is_strictly_exclusive(db_engine, ticker_id):
    """Boundary: a bar with timestamp exactly equal to `since` should NOT
    appear in the result. This matters for the scheduler — it remembers
    'I've ingested up to X' and asks for everything strictly after X."""
    with transaction(db_engine) as conn:
        BarsRepository(conn).insert_bar(ticker_id, Timeframe.DAILY, _bar(3))
    cutoff = datetime(2024, 1, 3, tzinfo=UTC)
    with db_engine.connect() as conn:
        bars = BarsRepository(conn).fetch_since(ticker_id, Timeframe.DAILY, cutoff)
    assert bars == []


def test_latest_timestamp(db_engine, ticker_id):
    with transaction(db_engine) as conn:
        repo = BarsRepository(conn)
        repo.insert_bar(ticker_id, Timeframe.DAILY, _bar(1))
        repo.insert_bar(ticker_id, Timeframe.DAILY, _bar(5))
        repo.insert_bar(ticker_id, Timeframe.DAILY, _bar(3))
    with db_engine.connect() as conn:
        ts = BarsRepository(conn).latest_timestamp(ticker_id, Timeframe.DAILY)
    assert ts == datetime(2024, 1, 5, tzinfo=UTC)


def test_latest_timestamp_none_when_empty(db_engine, ticker_id):
    with db_engine.connect() as conn:
        ts = BarsRepository(conn).latest_timestamp(ticker_id, Timeframe.DAILY)
    assert ts is None


def test_different_timeframes_are_isolated(db_engine, ticker_id):
    """Bars at different timeframes for the same ticker shouldn't bleed
    into each other's queries."""
    with transaction(db_engine) as conn:
        repo = BarsRepository(conn)
        repo.insert_bar(ticker_id, Timeframe.DAILY, _bar(1, close=100))
        repo.insert_bar(ticker_id, Timeframe.WEEKLY, _bar(1, close=200))
    with db_engine.connect() as conn:
        daily = BarsRepository(conn).fetch_history(ticker_id, Timeframe.DAILY)
        weekly = BarsRepository(conn).fetch_history(ticker_id, Timeframe.WEEKLY)
    assert len(daily) == 1 and daily[0].close == 100
    assert len(weekly) == 1 and weekly[0].close == 200


def test_upsert_updates_existing_bar(db_engine, ticker_id):
    bar_v1 = _bar(1, close=100)
    bar_v2 = OHLCVBar(
        timestamp=bar_v1.timestamp,
        open=110, high=115, low=109, close=112, volume=2000,
    )
    with transaction(db_engine) as conn:
        repo = BarsRepository(conn)
        repo.upsert_bar(ticker_id, Timeframe.DAILY, bar_v1)
        repo.upsert_bar(ticker_id, Timeframe.DAILY, bar_v2)   # same timestamp
    with db_engine.connect() as conn:
        bars = BarsRepository(conn).fetch_history(ticker_id, Timeframe.DAILY)
    assert len(bars) == 1
    assert bars[0].close == 112
    assert bars[0].volume == 2000
