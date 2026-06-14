"""Regression test for trip_ema.gui.queries.recent_signal_events.

The bug: the SQL referenced `SELECT ... signal ... FROM signal_events`,
but the canonical column name is `signal_type`. The query was unreached
by any test (the file was importable, the dataclass matched, but no
test ever sent it through the wire to a real Postgres). At runtime the
GUI's ticker detail panel logged `column "signal" does not exist`
every time the user selected a ticker.

This test runs the GUI query against the same Postgres fixture used by
the persistence repo tests, with a real signal_events row inserted via
the production repo. If the SELECT column list ever drifts from the
schema again, this fails immediately.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.domain import (
    ChangeSignal,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.gui.queries import recent_signal_events
from trip_ema.persistence import (
    SignalsRepository,
    TickersRepository,
    transaction,
)


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def ticker_id(db_engine, now):
    with transaction(db_engine) as conn:
        return TickersRepository(conn).insert(
            TickerRef("NSE", "RELIANCE", "EQ"), created_at=now,
        )


def test_recent_signal_events_returns_inserted_rows(db_engine, ticker_id, now):
    """The query should return the rows the production repo inserted,
    in newest-first order, with the signal field populated."""
    ref = TickerRef("NSE", "RELIANCE", "EQ")
    events = [
        SignalEvent(ref, Timeframe.WEEKLY, now - timedelta(days=14),
                    ChangeSignal.BUY, 2400.0, now - timedelta(days=14)),
        SignalEvent(ref, Timeframe.WEEKLY, now - timedelta(days=7),
                    ChangeSignal.EXIT_FROM_BUY, 2420.0, now - timedelta(days=7)),
        SignalEvent(ref, Timeframe.WEEKLY, now,
                    ChangeSignal.SELL, 2390.0, now),
    ]
    with transaction(db_engine) as conn:
        repo = SignalsRepository(conn)
        for e in events:
            repo.insert_event(ticker_id, e)

    with db_engine.connect() as conn:
        rows = recent_signal_events(conn, ticker_id, limit=10)

    # 3 inserted, all should come back.
    assert len(rows) == 3
    # Order is bar_timestamp DESC.
    assert [r.signal for r in rows] == ["SELL", "EXIT_FROM_BUY", "BUY"]
    # Numeric prices come back as float (the query casts via float()).
    assert all(isinstance(r.price_at_signal, float) for r in rows)
    # Each row has a non-empty timeframe and a real timestamp.
    for r in rows:
        assert r.timeframe
        assert r.bar_timestamp is not None
        assert r.event_id > 0


def test_recent_signal_events_respects_limit(db_engine, ticker_id, now):
    ref = TickerRef("NSE", "RELIANCE", "EQ")
    with transaction(db_engine) as conn:
        repo = SignalsRepository(conn)
        for i in range(5):
            repo.insert_event(ticker_id, SignalEvent(
                ref, Timeframe.WEEKLY,
                now - timedelta(days=7 * i),
                ChangeSignal.BUY,
                100.0 + i,
                now - timedelta(days=7 * i),
            ))

    with db_engine.connect() as conn:
        rows = recent_signal_events(conn, ticker_id, limit=2)
    assert len(rows) == 2


def test_recent_signal_events_empty_for_unknown_ticker(db_engine):
    """No rows → empty list, not an error."""
    with db_engine.connect() as conn:
        rows = recent_signal_events(conn, ticker_id=999_999, limit=10)
    assert rows == []
