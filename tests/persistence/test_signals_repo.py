"""Tests for SignalsRepository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.domain import (
    BarState,
    ChangeSignal,
    CoreRegime,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.persistence import (
    SignalsRepository,
    SubscribersRepository,
    TickersRepository,
    transaction,
)


@pytest.fixture
def now():
    # Use the actual current time so the notification_log partitions
    # created by migration 004 cover any timestamps we write.
    return datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def ticker_ids(db_engine, now):
    """Create a couple of tickers; return {symbol: ticker_id}."""
    ids = {}
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        ids["NIFTY"] = repo.insert(
            TickerRef("NSE", "NIFTY", "INDEX"), created_at=now,
        )
        ids["RELIANCE"] = repo.insert(
            TickerRef("NSE", "RELIANCE", "EQ"), created_at=now,
        )
    return ids


def _state_for_upsert_unused_now(*args, **kwargs):
    pass


# (Helper removed along with the upsert_ticker_state tests; left as a
# stub to minimize diff churn if reintroduced.)


# ---------- insert_event ---------- #

def test_insert_event_returns_event_id(db_engine, ticker_ids, now):
    event = SignalEvent(
        ticker=TickerRef("NSE", "NIFTY", "INDEX"),
        timeframe=Timeframe.WEEKLY,
        bar_timestamp=now - timedelta(days=7),
        signal=ChangeSignal.BUY,
        price_at_signal=24500.0,
        computed_at=now,
    )
    with transaction(db_engine) as conn:
        eid = SignalsRepository(conn).insert_event(ticker_ids["NIFTY"], event)
    assert isinstance(eid, int) and eid > 0


def test_insert_no_change_event_raises(db_engine, ticker_ids, now):
    event = SignalEvent(
        ticker=TickerRef("NSE", "NIFTY", "INDEX"),
        timeframe=Timeframe.WEEKLY,
        bar_timestamp=now,
        signal=ChangeSignal.NO_CHANGE,
        price_at_signal=100.0,
        computed_at=now,
    )
    with pytest.raises(ValueError):
        with transaction(db_engine) as conn:
            SignalsRepository(conn).insert_event(ticker_ids["NIFTY"], event)


def test_recent_events_for_ticker(db_engine, ticker_ids, now):
    ref = TickerRef("NSE", "NIFTY", "INDEX")
    events = [
        SignalEvent(ref, Timeframe.WEEKLY, now - timedelta(days=14),
                    ChangeSignal.BUY, 24000.0, now - timedelta(days=14)),
        SignalEvent(ref, Timeframe.WEEKLY, now - timedelta(days=7),
                    ChangeSignal.EXIT_FROM_BUY, 24200.0, now - timedelta(days=7)),
        SignalEvent(ref, Timeframe.WEEKLY, now,
                    ChangeSignal.SELL, 23900.0, now),
    ]
    with transaction(db_engine) as conn:
        repo = SignalsRepository(conn)
        for e in events:
            repo.insert_event(ticker_ids["NIFTY"], e)

    with db_engine.connect() as conn:
        recent = SignalsRepository(conn).recent_events_for_ticker(
            ticker_ids["NIFTY"], ref,
        )
    # Newest first (per order_by computed_at desc)
    signals = [e.signal for e in recent]
    assert signals == [ChangeSignal.SELL, ChangeSignal.EXIT_FROM_BUY, ChangeSignal.BUY]


# (upsert_ticker_state tests removed — table dropped in migration 005.)


# ---------- find_pending_for_subscriber ---------- #

def test_pending_returns_events_for_subscribed_ticker(db_engine, ticker_ids, now):
    """Subscriber wants NIFTY only. Two events fired, one for each ticker.
    Only the NIFTY one should come back."""
    nifty_ref = TickerRef("NSE", "NIFTY", "INDEX")
    reliance_ref = TickerRef("NSE", "RELIANCE", "EQ")

    with transaction(db_engine) as conn:
        signals_repo = SignalsRepository(conn)
        signals_repo.insert_event(ticker_ids["NIFTY"], SignalEvent(
            nifty_ref, Timeframe.WEEKLY, now, ChangeSignal.BUY, 24000, now,
        ))
        signals_repo.insert_event(ticker_ids["RELIANCE"], SignalEvent(
            reliance_ref, Timeframe.WEEKLY, now, ChangeSignal.SELL, 2400, now,
        ))

        subs_repo = SubscribersRepository(conn)
        sid = subs_repo.create_subscriber(
            transport="email", address="x@y.com",
            notify_cadence_minutes=15, created_at=now,
        )
        subs_repo.add_subscription(
            subscriber_id=sid, ticker_id=ticker_ids["NIFTY"],
            signal_filter=None, created_at=now,
        )

    with db_engine.connect() as conn:
        pending = SignalsRepository(conn).find_pending_for_subscriber(
            sid, now=now + timedelta(minutes=1),
        )
    refs = [r for _, _, r in pending]
    assert refs == [nifty_ref]


def test_pending_respects_signal_filter(db_engine, ticker_ids, now):
    """Subscriber wants only BUY signals. SELL events must be filtered out."""
    nifty_ref = TickerRef("NSE", "NIFTY", "INDEX")
    with transaction(db_engine) as conn:
        repo = SignalsRepository(conn)
        repo.insert_event(ticker_ids["NIFTY"], SignalEvent(
            nifty_ref, Timeframe.WEEKLY, now, ChangeSignal.BUY, 24000, now,
        ))
        repo.insert_event(ticker_ids["NIFTY"], SignalEvent(
            nifty_ref, Timeframe.WEEKLY, now, ChangeSignal.SELL, 23800, now,
        ))
        subs = SubscribersRepository(conn)
        sid = subs.create_subscriber(
            transport="email", address="x@y.com",
            notify_cadence_minutes=15, created_at=now,
        )
        subs.add_subscription(
            subscriber_id=sid, ticker_id=ticker_ids["NIFTY"],
            signal_filter="BUY", created_at=now,
        )

    with db_engine.connect() as conn:
        pending = SignalsRepository(conn).find_pending_for_subscriber(
            sid, now=now + timedelta(minutes=1),
        )
    signals = [e.signal for _, e, _ in pending]
    assert signals == [ChangeSignal.BUY]


def test_pending_respects_max_age(db_engine, ticker_ids, now):
    """An event older than max_age must not appear."""
    nifty_ref = TickerRef("NSE", "NIFTY", "INDEX")
    old_time = now - timedelta(hours=48)
    with transaction(db_engine) as conn:
        SignalsRepository(conn).insert_event(ticker_ids["NIFTY"], SignalEvent(
            nifty_ref, Timeframe.WEEKLY, old_time, ChangeSignal.BUY, 24000, old_time,
        ))
        subs = SubscribersRepository(conn)
        sid = subs.create_subscriber(
            transport="email", address="x@y.com",
            notify_cadence_minutes=15, created_at=now,
        )
        subs.add_subscription(
            subscriber_id=sid, ticker_id=None, signal_filter=None, created_at=now,
        )

    with db_engine.connect() as conn:
        pending = SignalsRepository(conn).find_pending_for_subscriber(
            sid, now=now, max_age=timedelta(hours=24),
        )
    assert pending == []


def test_pending_excludes_events_at_or_above_notification_cap(db_engine, ticker_ids, now):
    """Once a subscriber has been notified the maximum number of times for
    an event, that event must not appear in subsequent pending queries."""
    nifty_ref = TickerRef("NSE", "NIFTY", "INDEX")
    with transaction(db_engine) as conn:
        eid = SignalsRepository(conn).insert_event(ticker_ids["NIFTY"], SignalEvent(
            nifty_ref, Timeframe.WEEKLY, now, ChangeSignal.BUY, 24000, now,
        ))
        subs = SubscribersRepository(conn)
        sid = subs.create_subscriber(
            transport="email", address="x@y.com",
            notify_cadence_minutes=15, created_at=now,
        )
        subs.add_subscription(
            subscriber_id=sid, ticker_id=None, signal_filter=None, created_at=now,
        )
        # Record 3 notifications already sent
        for i in range(3):
            subs.record_notification(
                subscriber_id=sid, event_id=eid, sent_at=now + timedelta(minutes=i),
            )

    with db_engine.connect() as conn:
        pending = SignalsRepository(conn).find_pending_for_subscriber(
            sid, now=now + timedelta(minutes=5),
            max_notifications_each=3,
        )
    assert pending == []


def test_pending_includes_event_below_notification_cap(db_engine, ticker_ids, now):
    """An event with only 1 prior notification should still appear when the
    cap is 3."""
    nifty_ref = TickerRef("NSE", "NIFTY", "INDEX")
    with transaction(db_engine) as conn:
        eid = SignalsRepository(conn).insert_event(ticker_ids["NIFTY"], SignalEvent(
            nifty_ref, Timeframe.WEEKLY, now, ChangeSignal.BUY, 24000, now,
        ))
        subs = SubscribersRepository(conn)
        sid = subs.create_subscriber(
            transport="email", address="x@y.com",
            notify_cadence_minutes=15, created_at=now,
        )
        subs.add_subscription(
            subscriber_id=sid, ticker_id=None, signal_filter=None, created_at=now,
        )
        subs.record_notification(subscriber_id=sid, event_id=eid, sent_at=now)

    with db_engine.connect() as conn:
        pending = SignalsRepository(conn).find_pending_for_subscriber(
            sid, now=now + timedelta(minutes=5),
            max_notifications_each=3,
        )
    assert len(pending) == 1


# ============================================================
# Stale-event suppression — the B3 invariant
# ============================================================

class TestStaleEventSuppression:
    """Once a newer event exists for a (ticker, timeframe), older events
    for the same group must be suppressed entirely, regardless of how
    many notification sends they have remaining."""

    def test_newer_event_suppresses_older_with_remaining_sends(
        self, db_engine, ticker_ids, now,
    ):
        """BUY fires at T, has 2 of 3 sends remaining. SELL fires at T+1h.
        Pending query at T+2h must return ONLY the SELL — the BUY is dead."""
        nifty_ref = TickerRef("NSE", "NIFTY", "INDEX")
        with transaction(db_engine) as conn:
            sigs = SignalsRepository(conn)
            buy_id = sigs.insert_event(ticker_ids["NIFTY"], SignalEvent(
                nifty_ref, Timeframe.WEEKLY, now - timedelta(hours=2),
                ChangeSignal.BUY, 24000, now - timedelta(hours=2),
            ))
            sell_id = sigs.insert_event(ticker_ids["NIFTY"], SignalEvent(
                nifty_ref, Timeframe.WEEKLY, now - timedelta(hours=1),
                ChangeSignal.SELL, 23800, now - timedelta(hours=1),
            ))
            subs = SubscribersRepository(conn)
            sid = subs.create_subscriber(
                transport="email", address="x@y.com",
                notify_cadence_minutes=15,
                created_at=now - timedelta(hours=3),
            )
            subs.add_subscription(
                subscriber_id=sid, ticker_id=None, signal_filter=None,
                created_at=now - timedelta(hours=3),
            )
            # BUY was notified once (so still has 2 sends remaining)
            subs.record_notification(
                subscriber_id=sid, event_id=buy_id, sent_at=now - timedelta(hours=2),
            )

        with db_engine.connect() as conn:
            pending = SignalsRepository(conn).find_pending_for_subscriber(
                sid, now=now,
            )
        # Only the SELL should come back; BUY is suppressed even with 2 remaining
        assert len(pending) == 1
        returned_event_id = pending[0][0]
        returned_signal = pending[0][1].signal
        assert returned_event_id == sell_id
        assert returned_signal == ChangeSignal.SELL

    def test_two_tickers_independent_event_lifecycles(
        self, db_engine, ticker_ids, now,
    ):
        """Stale-event suppression is scoped to (ticker, timeframe). A
        newer event on RELIANCE must not suppress an older event on NIFTY."""
        nifty_ref = TickerRef("NSE", "NIFTY", "INDEX")
        reliance_ref = TickerRef("NSE", "RELIANCE", "EQ")
        with transaction(db_engine) as conn:
            sigs = SignalsRepository(conn)
            sigs.insert_event(ticker_ids["NIFTY"], SignalEvent(
                nifty_ref, Timeframe.WEEKLY, now - timedelta(hours=2),
                ChangeSignal.BUY, 24000, now - timedelta(hours=2),
            ))
            sigs.insert_event(ticker_ids["RELIANCE"], SignalEvent(
                reliance_ref, Timeframe.WEEKLY, now - timedelta(hours=1),
                ChangeSignal.SELL, 2400, now - timedelta(hours=1),
            ))
            subs = SubscribersRepository(conn)
            sid = subs.create_subscriber(
                transport="email", address="x@y.com",
                notify_cadence_minutes=15,
                created_at=now - timedelta(hours=3),
            )
            subs.add_subscription(
                subscriber_id=sid, ticker_id=None, signal_filter=None,
                created_at=now - timedelta(hours=3),
            )

        with db_engine.connect() as conn:
            pending = SignalsRepository(conn).find_pending_for_subscriber(
                sid, now=now,
            )
        # Both should appear — different tickers
        assert len(pending) == 2
        signals = sorted(e.signal.name for _, e, _ in pending)
        assert signals == ["BUY", "SELL"]

    def test_same_signal_replayed_picks_latest(self, db_engine, ticker_ids, now):
        """Two BUY events for NIFTY (e.g., regime flickered out and back in
        on a different bar). The later BUY is the live one; the earlier
        is suppressed."""
        nifty_ref = TickerRef("NSE", "NIFTY", "INDEX")
        with transaction(db_engine) as conn:
            sigs = SignalsRepository(conn)
            early_id = sigs.insert_event(ticker_ids["NIFTY"], SignalEvent(
                nifty_ref, Timeframe.WEEKLY, now - timedelta(hours=2),
                ChangeSignal.BUY, 24000, now - timedelta(hours=2),
            ))
            late_id = sigs.insert_event(ticker_ids["NIFTY"], SignalEvent(
                nifty_ref, Timeframe.WEEKLY, now - timedelta(hours=1),
                ChangeSignal.BUY, 24100, now - timedelta(hours=1),
            ))
            subs = SubscribersRepository(conn)
            sid = subs.create_subscriber(
                transport="email", address="x@y.com",
                notify_cadence_minutes=15,
                created_at=now - timedelta(hours=3),
            )
            subs.add_subscription(
                subscriber_id=sid, ticker_id=None, signal_filter=None,
                created_at=now - timedelta(hours=3),
            )

        with db_engine.connect() as conn:
            pending = SignalsRepository(conn).find_pending_for_subscriber(
                sid, now=now,
            )
        assert len(pending) == 1
        assert pending[0][0] == late_id   # the later event wins
