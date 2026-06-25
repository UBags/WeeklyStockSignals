"""Tests for SubscriberDistributor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import pytest

from trip_ema.config.schema import MarketHoursConfig
from trip_ema.domain import (
    ChangeSignal,
    NotificationBatch,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.notifications import NullTransport
from trip_ema.persistence import (
    SignalsRepository,
    SubscribersRepository,
    TickersRepository,
    transaction,
)
from trip_ema.scheduler import MarketHoursService, SubscriberDistributor


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")
RELIANCE = TickerRef("NSE", "RELIANCE", "EQ")


@pytest.fixture
def now():
    return datetime.now(UTC).replace(microsecond=0)


# ---------- Recording transport: captures send() calls for inspection ---------- #

class RecordingTransport:
    """Fake transport that records every send() call and returns
    success/failure as configured."""

    def __init__(self, succeed: bool = True):
        self.succeed = succeed
        self.calls: list[tuple[str, NotificationBatch]] = []

    def send(self, address: str, batch: NotificationBatch) -> bool:
        self.calls.append((address, batch))
        return self.succeed


# ---------- Helpers ---------- #

def _market_hours_always_open() -> MarketHoursService:
    """MarketHoursService where NSE is open 24/7 (so tests don't depend on
    when they run)."""
    return MarketHoursService([
        MarketHoursConfig(
            exchange="NSE", open="00:00", close="23:59",
            tz="UTC", weekdays=[1, 2, 3, 4, 5, 6, 7],
        ),
    ])


def _market_hours_always_closed() -> MarketHoursService:
    """MarketHoursService configured for an exchange that's never open
    (weekdays=[1] for Monday only, narrow window) — tested by passing
    Tuesday."""
    return MarketHoursService([
        MarketHoursConfig(
            exchange="NSE", open="09:00", close="09:01",
            tz="UTC", weekdays=[1],
        ),
    ])


def _seed_subscriber_and_event(db_engine, now, *, cadence=15, signal_filter=None):
    """Create one subscriber, one ticker, one signal event. Returns
    (subscriber_id, event_id, ticker_id)."""
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(NIFTY, created_at=now)
        eid = SignalsRepository(conn).insert_event(tid, SignalEvent(
            ticker=NIFTY,
            timeframe=Timeframe.WEEKLY,
            bar_timestamp=now,
            signal=ChangeSignal.BUY,
            price_at_signal=24856.20,
            computed_at=now,
        ))
        sid = SubscribersRepository(conn).create_subscriber(
            transport="email", address="user@example.com",
            notify_cadence_minutes=cadence, created_at=now - timedelta(hours=1),
        )
        SubscribersRepository(conn).add_subscription(
            subscriber_id=sid, ticker_id=tid, signal_filter=signal_filter,
            created_at=now - timedelta(hours=1),
        )
    return sid, eid, tid


# ============================================================
# Market-hours gating
# ============================================================

def test_runs_regardless_of_market_state(db_engine, now):
    """B3 (Phase 8): the distributor no longer consults market hours.
    Even with an always-closed market, it delivers pending events.

    Rationale: the system informs subscribers ASAP. Tracking and acting
    on signals is the trader's responsibility, not ours.
    """
    sid, eid, _ = _seed_subscriber_and_event(db_engine, now)
    transport = RecordingTransport()

    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_closed(),
        transports={"email": transport},
    )
    summary = distributor.run_once()
    # The subscriber was created an hour ago with cadence=15, so it's due.
    assert summary.subscribers_due == 1
    assert summary.subscribers_notified == 1
    assert len(transport.calls) == 1


def test_does_not_take_market_hours_argument_anymore(db_engine, now):
    """The obsolete `respect_market_hours` and `suppress_stale_bar_dates`
    constructor params were removed. Passing them must raise TypeError —
    fail loudly rather than silently letting the old policy be reapplied."""
    with pytest.raises(TypeError):
        SubscriberDistributor(
            db_engine=db_engine,
            market_hours=_market_hours_always_open(),
            transports={},
            respect_market_hours=False,   # removed param
        )
    with pytest.raises(TypeError):
        SubscriberDistributor(
            db_engine=db_engine,
            market_hours=_market_hours_always_open(),
            transports={},
            suppress_stale_bar_dates=False,   # removed param
        )


# ============================================================
# Happy path
# ============================================================

def test_dispatches_pending_event_to_subscriber(db_engine, now):
    """One subscriber, one event, no prior notifications → batch sent."""
    sid, eid, _ = _seed_subscriber_and_event(db_engine, now)
    transport = RecordingTransport()

    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={"email": transport},
    )
    summary = distributor.run_once()
    assert summary.subscribers_notified == 1
    assert summary.events_dispatched == 1

    [(address, batch)] = transport.calls
    assert address == "user@example.com"
    assert len(batch.events) == 1
    assert batch.events[0].signal == ChangeSignal.BUY


def test_records_notification_log_after_successful_send(db_engine, now):
    """After delivery, notification_log should have a row for (sub, event)."""
    sid, eid, _ = _seed_subscriber_and_event(db_engine, now)
    transport = RecordingTransport()

    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={"email": transport},
    )
    distributor.run_once()

    from sqlalchemy import text
    with db_engine.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM notification_log "
            "WHERE subscriber_id = :sid AND event_id = :eid"
        ), {"sid": sid, "eid": eid}).scalar_one()
    assert count == 1


def test_updates_last_notified_at_after_successful_send(db_engine, now):
    sid, _, _ = _seed_subscriber_and_event(db_engine, now)
    transport = RecordingTransport()

    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={"email": transport},
    )
    distributor.run_once()

    with db_engine.connect() as conn:
        rec = SubscribersRepository(conn).get(sid)
    assert rec.last_notified_at is not None


# ============================================================
# Batching across multiple events
# ============================================================

def test_batches_multiple_events_into_one_message(db_engine, now):
    """A subscriber with two pending events on different tickers should
    receive one batched message, not two separate ones.

    Note: two events on the SAME ticker would now be stale-suppressed
    (the newer event hides the older), so this test uses different
    tickers to verify the batching mechanic itself.
    """
    with transaction(db_engine) as conn:
        nifty_id = TickersRepository(conn).insert(NIFTY, created_at=now)
        reliance_id = TickersRepository(conn).insert(RELIANCE, created_at=now)
        signals_repo = SignalsRepository(conn)
        signals_repo.insert_event(nifty_id, SignalEvent(
            ticker=NIFTY, timeframe=Timeframe.WEEKLY,
            bar_timestamp=now - timedelta(minutes=30),
            signal=ChangeSignal.BUY, price_at_signal=24800,
            computed_at=now - timedelta(minutes=30),
        ))
        signals_repo.insert_event(reliance_id, SignalEvent(
            ticker=RELIANCE, timeframe=Timeframe.WEEKLY,
            bar_timestamp=now - timedelta(minutes=15),
            signal=ChangeSignal.SELL, price_at_signal=2400,
            computed_at=now - timedelta(minutes=15),
        ))
        subs = SubscribersRepository(conn)
        sid = subs.create_subscriber(
            transport="email", address="user@example.com",
            notify_cadence_minutes=15, created_at=now - timedelta(hours=1),
        )
        # Subscribe to ALL tickers (no ticker filter)
        subs.add_subscription(
            subscriber_id=sid, ticker_id=None, signal_filter=None,
            created_at=now - timedelta(hours=1),
        )

    transport = RecordingTransport()
    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={"email": transport},
    )
    distributor.run_once()
    assert len(transport.calls) == 1
    [(_, batch)] = transport.calls
    assert len(batch.events) == 2
    # Events should be one per ticker
    tickers_in_batch = {e.ticker.symbol for e in batch.events}
    assert tickers_in_batch == {"NIFTY", "RELIANCE"}


# ============================================================
# Repeat-firing cap
# ============================================================

def test_event_dispatched_at_most_three_times_per_subscriber(db_engine, now):
    """After 3 dispatches for the same event, subsequent runs should not
    re-send it. Tests the SQL filter."""
    sid, eid, _ = _seed_subscriber_and_event(db_engine, now, cadence=15)
    transport = RecordingTransport()

    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={"email": transport},
    )

    # Run 4 times, manually resetting last_notified_at between runs so
    # the cadence check doesn't block us. The notification cap (3) should
    # block run #4.
    for i in range(4):
        distributor.run_once()
        if i < 3:
            with transaction(db_engine) as conn:
                SubscribersRepository(conn).update_last_notified(
                    sid, now - timedelta(hours=2),
                )

    # Should have sent on runs 1, 2, 3; skipped on run 4.
    assert len(transport.calls) == 3


# ============================================================
# Cadence respected
# ============================================================

def test_recently_notified_subscriber_not_dispatched_again(db_engine, now):
    """A subscriber notified within their cadence window should be skipped
    on the next pass."""
    sid, _, _ = _seed_subscriber_and_event(db_engine, now, cadence=60)
    transport = RecordingTransport()
    # Mark them as just-notified
    with transaction(db_engine) as conn:
        SubscribersRepository(conn).update_last_notified(
            sid, now - timedelta(minutes=5),
        )

    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={"email": transport},
    )
    summary = distributor.run_once()
    assert summary.subscribers_due == 0
    assert transport.calls == []


# ============================================================
# Failure handling
# ============================================================

def test_failed_transport_does_not_record_notification(db_engine, now):
    """If the transport reports failure, no notification_log row should be
    written, so the event remains 'pending' for next cycle."""
    sid, eid, _ = _seed_subscriber_and_event(db_engine, now)
    transport = RecordingTransport(succeed=False)

    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={"email": transport},
    )
    distributor.run_once()

    from sqlalchemy import text
    with db_engine.connect() as conn:
        count = conn.execute(text(
            "SELECT COUNT(*) FROM notification_log "
            "WHERE subscriber_id = :sid AND event_id = :eid"
        ), {"sid": sid, "eid": eid}).scalar_one()
    assert count == 0


def test_failed_transport_does_not_update_last_notified(db_engine, now):
    sid, _, _ = _seed_subscriber_and_event(db_engine, now)
    transport = RecordingTransport(succeed=False)

    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={"email": transport},
    )
    distributor.run_once()

    with db_engine.connect() as conn:
        rec = SubscribersRepository(conn).get(sid)
    assert rec.last_notified_at is None  # never set


def test_unknown_transport_falls_back_to_null(db_engine, now):
    """If a subscriber's transport isn't registered, the distributor uses
    the NullTransport (which always succeeds), records the notification,
    and moves on. This is a defensive default — operators can monitor
    for subscribers with bad transport configs in the log_table."""
    sid, eid, _ = _seed_subscriber_and_event(db_engine, now)
    # transports dict is EMPTY — no "email" transport registered
    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={},
    )
    summary = distributor.run_once()
    # The null transport returns True so the notification IS recorded.
    assert summary.subscribers_notified == 1


# ============================================================
# Cadence validation
# ============================================================

def test_cadence_below_one_minute_rejected(db_engine):
    with pytest.raises(ValueError):
        SubscriberDistributor(
            db_engine=db_engine,
            market_hours=_market_hours_always_open(),
            transports={},
            cadence_minutes=0,
        )


# ============================================================
# Phase 6: stale-bar-date suppression
# ============================================================


class TestB3DeliveryRegardlessOfBarBucket:
    """Phase 8 B3: events deliver until `max_event_age_hours` cuts them
    off — no longer suppressed because the bar's bucket has closed."""

    def test_event_from_closed_week_within_age_window_dispatched(self, db_engine):
        """An event whose weekly bucket closed yesterday but is still
        inside the max_event_age window should be dispatched.

        This is the Friday-15:00-signal-on-Saturday case: trader missed
        it during the week, distributor still tries Saturday morning
        because age-cap hasn't expired."""
        now = datetime(2026, 5, 18, 12, tzinfo=UTC)  # Mon
        event_bar_ts = datetime(2026, 5, 11, tzinfo=UTC)  # last Mon
        # Event computed 18 hours ago (well inside the default 24h cap)
        event_computed = now - timedelta(hours=18)

        with transaction(db_engine) as conn:
            tid = TickersRepository(conn).insert(NIFTY, created_at=now)
            SignalsRepository(conn).insert_event(tid, SignalEvent(
                ticker=NIFTY, timeframe=Timeframe.WEEKLY,
                bar_timestamp=event_bar_ts,
                signal=ChangeSignal.BUY, price_at_signal=24000,
                computed_at=event_computed,
            ))
            subs = SubscribersRepository(conn)
            sid = subs.create_subscriber(
                transport="email", address="x@y.com",
                notify_cadence_minutes=15,
                created_at=now - timedelta(days=30),
            )
            subs.add_subscription(
                subscriber_id=sid, ticker_id=None, signal_filter=None,
                created_at=now - timedelta(days=30),
            )

        transport = RecordingTransport()
        distributor = SubscriberDistributor(
            db_engine=db_engine,
            market_hours=_market_hours_always_open(),
            transports={"email": transport},
        )
        from unittest.mock import patch
        with patch(
            "trip_ema.scheduler.subscriber_distributor.datetime",
            wraps=datetime, now=lambda tz=None: now,
        ):
            summary = distributor.run_once()
        # Phase 6 used to suppress this. Phase 8 B3 lets it through.
        assert summary.events_dispatched == 1
        assert len(transport.calls) == 1

    def test_event_beyond_max_age_still_suppressed_by_age_cap(self, db_engine):
        """The single remaining freshness gate is `max_event_age_hours`.
        An event 48 hours old with max_age=24h should not deliver."""
        now = datetime(2026, 5, 18, 12, tzinfo=UTC)
        event_computed = now - timedelta(hours=48)

        with transaction(db_engine) as conn:
            tid = TickersRepository(conn).insert(NIFTY, created_at=now)
            SignalsRepository(conn).insert_event(tid, SignalEvent(
                ticker=NIFTY, timeframe=Timeframe.WEEKLY,
                bar_timestamp=event_computed,
                signal=ChangeSignal.BUY, price_at_signal=24000,
                computed_at=event_computed,
            ))
            subs = SubscribersRepository(conn)
            sid = subs.create_subscriber(
                transport="email", address="x@y.com",
                notify_cadence_minutes=15,
                created_at=now - timedelta(days=30),
            )
            subs.add_subscription(
                subscriber_id=sid, ticker_id=None, signal_filter=None,
                created_at=now - timedelta(days=30),
            )

        transport = RecordingTransport()
        distributor = SubscriberDistributor(
            db_engine=db_engine,
            market_hours=_market_hours_always_open(),
            transports={"email": transport},
            max_event_age_hours=24,
        )
        from unittest.mock import patch
        with patch(
            "trip_ema.scheduler.subscriber_distributor.datetime",
            wraps=datetime, now=lambda tz=None: now,
        ):
            summary = distributor.run_once()
        assert summary.events_dispatched == 0
        assert len(transport.calls) == 0


# ============================================================
# Phase 6: last_run_completed_at and is_alive
# ============================================================

def test_last_run_completed_at_set_after_run(db_engine):
    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={},
    )
    assert distributor.last_run_completed_at is None
    distributor.run_once()
    assert distributor.last_run_completed_at is not None


def test_is_alive_false_before_start(db_engine):
    distributor = SubscriberDistributor(
        db_engine=db_engine,
        market_hours=_market_hours_always_open(),
        transports={},
    )
    assert distributor.is_alive() is False


# ============================================================
# Phase 7: queue mode (Redis enqueue instead of in-process dispatch)
# ============================================================

import fakeredis
from trip_ema.notifications.redis_queue import RedisQueue


def _make_queue():
    r = fakeredis.FakeStrictRedis()
    return RedisQueue(r, "t:q", "t:dedup")


class TestQueueMode:
    """When the distributor is configured with a queue, it enqueues
    one job per event instead of calling transports synchronously."""

    def test_enqueues_one_job_per_event(self, db_engine, now):
        with transaction(db_engine) as conn:
            tid = TickersRepository(conn).insert(NIFTY, created_at=now)
            SignalsRepository(conn).insert_event(tid, SignalEvent(
                ticker=NIFTY, timeframe=Timeframe.WEEKLY,
                bar_timestamp=now - timedelta(minutes=30),
                signal=ChangeSignal.BUY, price_at_signal=24000,
                computed_at=now - timedelta(minutes=30),
            ))
            subs = SubscribersRepository(conn)
            sid = subs.create_subscriber(
                transport="email", address="u@x.com",
                notify_cadence_minutes=15,
                created_at=now - timedelta(hours=1),
            )
            subs.add_subscription(
                subscriber_id=sid, ticker_id=None, signal_filter=None,
                created_at=now - timedelta(hours=1),
            )

        queue = _make_queue()
        distributor = SubscriberDistributor(
            db_engine=db_engine,
            market_hours=_market_hours_always_open(),
            queue=queue,
        )
        summary = distributor.run_once()
        assert summary.events_enqueued == 1
        assert summary.events_dispatched == 0   # nothing dispatched in-process
        assert queue.queue_depth() == 1
        # The single job should now be sitting in Redis
        popped = queue.pop_blocking(1)
        assert popped["subscriber_id"] == sid
        assert popped["event"]["signal"] == "BUY"

    def test_does_not_call_transports_in_queue_mode(self, db_engine, now):
        with transaction(db_engine) as conn:
            tid = TickersRepository(conn).insert(NIFTY, created_at=now)
            SignalsRepository(conn).insert_event(tid, SignalEvent(
                ticker=NIFTY, timeframe=Timeframe.WEEKLY,
                bar_timestamp=now - timedelta(minutes=30),
                signal=ChangeSignal.BUY, price_at_signal=24000,
                computed_at=now - timedelta(minutes=30),
            ))
            subs = SubscribersRepository(conn)
            sid = subs.create_subscriber(
                transport="email", address="u@x.com",
                notify_cadence_minutes=15,
                created_at=now - timedelta(hours=1),
            )
            subs.add_subscription(
                subscriber_id=sid, ticker_id=None, signal_filter=None,
                created_at=now - timedelta(hours=1),
            )

        transport = RecordingTransport()
        queue = _make_queue()
        distributor = SubscriberDistributor(
            db_engine=db_engine,
            market_hours=_market_hours_always_open(),
            transports={"email": transport},   # provided but ignored in queue mode
            queue=queue,
        )
        distributor.run_once()
        # transport.send() was never called — the worker would do it
        assert transport.calls == []

    def test_dedup_blocks_double_enqueue_in_same_cycle(self, db_engine, now):
        """If the same (sub, event) is already in flight in the queue,
        a second enqueue attempt is dedup-blocked."""
        with transaction(db_engine) as conn:
            tid = TickersRepository(conn).insert(NIFTY, created_at=now)
            eid = SignalsRepository(conn).insert_event(tid, SignalEvent(
                ticker=NIFTY, timeframe=Timeframe.WEEKLY,
                bar_timestamp=now - timedelta(minutes=30),
                signal=ChangeSignal.BUY, price_at_signal=24000,
                computed_at=now - timedelta(minutes=30),
            ))
            subs = SubscribersRepository(conn)
            sid = subs.create_subscriber(
                transport="email", address="u@x.com",
                notify_cadence_minutes=15,
                created_at=now - timedelta(hours=1),
            )
            subs.add_subscription(
                subscriber_id=sid, ticker_id=None, signal_filter=None,
                created_at=now - timedelta(hours=1),
            )

        queue = _make_queue()
        # Pre-fill the dedup slot for this job_id
        queue.enqueue(f"{sid}:{eid}", {"placeholder": True})

        distributor = SubscriberDistributor(
            db_engine=db_engine,
            market_hours=_market_hours_always_open(),
            queue=queue,
        )
        summary = distributor.run_once()
        assert summary.events_enqueued == 0
        assert summary.events_skipped_dedup == 1

    def test_writes_no_notification_log_in_queue_mode(self, db_engine, now):
        """notification_log writes are the worker's responsibility now."""
        with transaction(db_engine) as conn:
            tid = TickersRepository(conn).insert(NIFTY, created_at=now)
            eid = SignalsRepository(conn).insert_event(tid, SignalEvent(
                ticker=NIFTY, timeframe=Timeframe.WEEKLY,
                bar_timestamp=now - timedelta(minutes=30),
                signal=ChangeSignal.BUY, price_at_signal=24000,
                computed_at=now - timedelta(minutes=30),
            ))
            subs = SubscribersRepository(conn)
            sid = subs.create_subscriber(
                transport="email", address="u@x.com",
                notify_cadence_minutes=15,
                created_at=now - timedelta(hours=1),
            )
            subs.add_subscription(
                subscriber_id=sid, ticker_id=None, signal_filter=None,
                created_at=now - timedelta(hours=1),
            )

        queue = _make_queue()
        distributor = SubscriberDistributor(
            db_engine=db_engine,
            market_hours=_market_hours_always_open(),
            queue=queue,
        )
        distributor.run_once()

        from sqlalchemy import text
        with db_engine.connect() as conn:
            count = conn.execute(text(
                "SELECT count(*) FROM notification_log WHERE subscriber_id = :sid"
            ), {"sid": sid}).scalar()
        assert count == 0   # distributor did not write; worker hasn't run


def test_distributor_requires_queue_or_transports(db_engine):
    """Must pass either queue or transports — not both missing."""
    with pytest.raises(ValueError):
        SubscriberDistributor(
            db_engine=db_engine,
            market_hours=_market_hours_always_open(),
        )
