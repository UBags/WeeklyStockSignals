"""Regression tests for trip_ema.gui.queries.recent_signal_events.

Step 12a: V1 read path. The function now reads current_signals +
historical_signals (no JOIN, two separate queries composed in
Python) instead of the legacy `signal_events` table.

Strategy under test (operator's spec):
  1. Check current_signals for a row for this ticker. If found,
     take it as row #1 (newest).
  2. Top up from historical_signals (bar_timestamp DESC,
     computed_at DESC) for the remaining (limit - 1) rows.
  3. If current_signals had no match, take all `limit` rows from
     historical_signals.

SignalEventRow.event_id: int | None  — None for the current_signals
row; the actual surrogate id (from historical_signals.signal_id)
for historical rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from trip_ema.domain import TickerRef
from trip_ema.domain.signal_event import SignalEvent
from trip_ema.gui.queries import recent_signal_events
from trip_ema.persistence import TickersRepository, transaction
from trip_ema.persistence.current_signals_repo import (
    CurrentSignalsRepository,
)
from trip_ema.persistence.historical_signals_repo import (
    HistoricalSignalsRepository,
)


@pytest.fixture
def now():
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def ticker_id(db_engine, now):
    with transaction(db_engine) as conn:
        return TickersRepository(conn).insert(
            TickerRef("NSE", "RELIANCE", "EQ"), created_at=now,
        )


def _make_event(
    *,
    ticker_id: int,
    bar_ts: datetime,
    signal_type: str = "BUY",
    price: float = 100.0,
    computed_at: datetime | None = None,
) -> SignalEvent:
    """Build a V1 SignalEvent. regime_before/after are chosen so
    the CHECK constraint passes (must differ); the exact values
    don't matter for the GUI read tests."""
    regime_map = {
        "BUY":            (-1,  1),
        "SELL":           ( 1, -1),
        "EXIT_FROM_BUY":  ( 1,  0),
        "EXIT_FROM_SELL": (-1,  0),
    }
    regime_before, regime_after = regime_map[signal_type]
    return SignalEvent(
        ticker_id=ticker_id,
        timeframe="1W",
        bar_timestamp=bar_ts,
        signal_type=signal_type,
        regime_before=regime_before,
        regime_after=regime_after,
        price_at_signal=price,
        computed_at=computed_at or bar_ts,
    )


# ============================================================
# Existing scenarios — V1-rewired seeds
# ============================================================

def test_recent_signal_events_returns_historical_only(
    db_engine, ticker_id, now,
):
    """When current_signals has no row for this ticker, all output
    rows come from historical_signals (newest-first by
    bar_timestamp, computed_at)."""
    bars = [
        now - timedelta(days=14),
        now - timedelta(days=7),
        now,
    ]
    events = [
        _make_event(
            ticker_id=ticker_id, bar_ts=bars[0],
            signal_type="BUY", price=2400.0,
        ),
        _make_event(
            ticker_id=ticker_id, bar_ts=bars[1],
            signal_type="EXIT_FROM_BUY", price=2420.0,
        ),
        _make_event(
            ticker_id=ticker_id, bar_ts=bars[2],
            signal_type="SELL", price=2390.0,
        ),
    ]
    with transaction(db_engine) as conn:
        HistoricalSignalsRepository(conn).insert_many(events)

    with db_engine.connect() as conn:
        rows = recent_signal_events(conn, ticker_id, limit=10)

    assert len(rows) == 3
    # Newest first by bar_timestamp
    assert [r.signal for r in rows] == [
        "SELL", "EXIT_FROM_BUY", "BUY",
    ]
    # Prices come back as float
    assert all(isinstance(r.price_at_signal, float) for r in rows)
    # Each historical row has a real event_id (>= 1, from BIGSERIAL)
    for r in rows:
        assert r.event_id is not None and r.event_id >= 1
        assert r.timeframe == "1W"
        assert r.bar_timestamp is not None


def test_recent_signal_events_current_plus_historical(
    db_engine, ticker_id, now,
):
    """When current_signals has a row, it appears FIRST (newest),
    then historical rows are appended up to the limit. The
    current_signals row carries event_id=None."""
    current_bar = now
    historical_bars = [
        now - timedelta(days=21),
        now - timedelta(days=14),
        now - timedelta(days=7),
    ]
    with transaction(db_engine) as conn:
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=ticker_id, bar_ts=current_bar,
                signal_type="SELL", price=2390.0,
            ),
        ])
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=ticker_id, bar_ts=b,
                signal_type="BUY", price=2400.0,
            )
            for b in historical_bars
        ])

    with db_engine.connect() as conn:
        rows = recent_signal_events(conn, ticker_id, limit=5)

    # Order: current row first, then historical newest-first.
    assert len(rows) == 4
    assert rows[0].event_id is None
    assert rows[0].signal == "SELL"
    assert rows[0].bar_timestamp == current_bar
    # Historical rows (newest first by bar_timestamp DESC)
    assert all(r.event_id is not None for r in rows[1:])
    assert [r.bar_timestamp for r in rows[1:]] == [
        historical_bars[2],
        historical_bars[1],
        historical_bars[0],
    ]


def test_recent_signal_events_respects_limit(
    db_engine, ticker_id, now,
):
    """limit=2 → at most 2 rows, even if both tables have more."""
    with transaction(db_engine) as conn:
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=ticker_id, bar_ts=now,
                signal_type="BUY",
            ),
        ])
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=ticker_id,
                bar_ts=now - timedelta(days=7 * i),
                signal_type="BUY",
            )
            for i in range(1, 6)
        ])

    with db_engine.connect() as conn:
        rows = recent_signal_events(conn, ticker_id, limit=2)
    assert len(rows) == 2
    # First is current (no event_id), second is the most recent
    # historical
    assert rows[0].event_id is None
    assert rows[1].event_id is not None


def test_recent_signal_events_limit_one_returns_only_current(
    db_engine, ticker_id, now,
):
    """When current_signals has a row and limit=1, the historical
    query should not be issued at all (verified via the result
    shape — only the current row appears)."""
    with transaction(db_engine) as conn:
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=ticker_id, bar_ts=now,
                signal_type="BUY",
            ),
        ])
        HistoricalSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=ticker_id,
                bar_ts=now - timedelta(days=7),
                signal_type="SELL",
            ),
        ])

    with db_engine.connect() as conn:
        rows = recent_signal_events(conn, ticker_id, limit=1)
    assert len(rows) == 1
    assert rows[0].event_id is None
    assert rows[0].signal == "BUY"


def test_recent_signal_events_empty_for_unknown_ticker(db_engine):
    """No rows in either table → empty list, not an error."""
    with db_engine.connect() as conn:
        rows = recent_signal_events(
            conn, ticker_id=999_999, limit=10,
        )
    assert rows == []


def test_recent_signal_events_zero_limit_returns_empty(
    db_engine, ticker_id, now,
):
    """limit=0 → empty list, no queries issued."""
    with transaction(db_engine) as conn:
        CurrentSignalsRepository(conn).insert_many([
            _make_event(
                ticker_id=ticker_id, bar_ts=now, signal_type="BUY",
            ),
        ])
    with db_engine.connect() as conn:
        rows = recent_signal_events(conn, ticker_id, limit=0)
    assert rows == []
