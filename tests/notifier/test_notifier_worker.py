"""Tests for NotifierWorker — the queue consumer that runs in a separate process."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from sqlalchemy import text

from trip_ema.domain import (
    ChangeSignal,
    NotificationBatch,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.notifications.base import NotificationTransport
from trip_ema.notifications.redis_queue import RedisQueue
from trip_ema.notifier_worker import NotifierWorker
from trip_ema.persistence import (
    SignalsRepository,
    SubscribersRepository,
    TickersRepository,
    transaction,
)


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")


class RecordingTransport:
    """Captures send() calls. Returns True by default; can be configured
    to fail by setting `should_fail = True`."""
    def __init__(self):
        self.calls: list[tuple[str, NotificationBatch]] = []
        self.should_fail = False

    def send(self, address: str, batch: NotificationBatch) -> bool:
        self.calls.append((address, batch))
        return not self.should_fail


# ---------- Fixtures ---------- #

@pytest.fixture
def redis_client():
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def queue(redis_client):
    return RedisQueue(
        redis_client=redis_client,
        queue_name="test:jobs",
        dedup_hash_name="test:dedup",
    )


@pytest.fixture
def now():
    return datetime(2026, 5, 18, 12, tzinfo=timezone.utc)


@pytest.fixture
def seeded(db_engine, now):
    """Seed a ticker, subscriber, subscription, and one signal event.
    Returns (subscriber_id, event_id, ticker_id)."""
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(NIFTY, created_at=now)
        eid = SignalsRepository(conn).insert_event(tid, SignalEvent(
            ticker=NIFTY, timeframe=Timeframe.WEEKLY,
            bar_timestamp=now - timedelta(hours=1),
            signal=ChangeSignal.BUY, price_at_signal=24000,
            computed_at=now - timedelta(hours=1),
        ))
        subs = SubscribersRepository(conn)
        sid = subs.create_subscriber(
            transport="email", address="x@y.com",
            notify_cadence_minutes=15,
            created_at=now - timedelta(hours=2),
        )
        subs.add_subscription(
            subscriber_id=sid, ticker_id=None, signal_filter=None,
            created_at=now - timedelta(hours=2),
        )
    return sid, eid, tid


def _build_job(subscriber_id: int, event_id: int) -> dict:
    """Construct a job payload as the distributor would."""
    return {
        "subscriber_id": subscriber_id,
        "transport": "email",
        "address": "x@y.com",
        "event_id": event_id,
        "event": {
            "ticker": {
                "exchange": "NSE",
                "symbol": "NIFTY",
                "instrument": "INDEX",
            },
            "timeframe": "1W",
            "bar_timestamp": "2026-05-18T11:00:00+00:00",
            "signal": "BUY",
            "price_at_signal": 24000.0,
            "computed_at": "2026-05-18T11:00:00+00:00",
        },
        "enqueued_at": "2026-05-18T12:00:00+00:00",
    }


# ---------- Successful delivery ---------- #


def test_successful_send_writes_log_and_acks(db_engine, queue, seeded):
    sid, eid, _ = seeded
    transport = RecordingTransport()
    worker = NotifierWorker(
        queue=queue, db_engine=db_engine,
        transports={"email": transport},
    )

    job_id = f"{sid}:{eid}"
    queue.enqueue(job_id, _build_job(sid, eid))
    assert queue.in_flight_count() == 1

    processed = worker.run_once_for_test(max_jobs=1)
    assert processed == 1
    assert worker.jobs_succeeded == 1
    assert worker.jobs_failed == 0

    # Transport got the call
    assert len(transport.calls) == 1
    addr, batch = transport.calls[0]
    assert addr == "x@y.com"
    assert len(batch.events) == 1
    assert batch.events[0].signal == ChangeSignal.BUY

    # Dedup slot cleared
    assert queue.in_flight_count() == 0

    # notification_log row written
    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT count(*) FROM notification_log "
            "WHERE subscriber_id = :sid AND event_id = :eid"
        ), {"sid": sid, "eid": eid}).scalar()
    assert rows == 1


def test_successful_send_updates_last_notified_at(db_engine, queue, seeded):
    sid, eid, _ = seeded
    worker = NotifierWorker(
        queue=queue, db_engine=db_engine,
        transports={"email": RecordingTransport()},
    )
    queue.enqueue(f"{sid}:{eid}", _build_job(sid, eid))
    worker.run_once_for_test(max_jobs=1)

    with db_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT last_notified_at FROM subscribers WHERE subscriber_id = :sid"
        ), {"sid": sid}).first()
    assert row.last_notified_at is not None


# ---------- Failed delivery ---------- #


def test_failed_send_does_not_ack(db_engine, queue, seeded):
    """A2 fix: on transport failure, the worker calls nack() to free the
    dedup slot immediately so the distributor's next cycle can re-enqueue.
    Previously the slot stayed held until the TTL sweep cleared it (up to
    1 hour), causing artificial retry delays."""
    sid, eid, _ = seeded
    transport = RecordingTransport()
    transport.should_fail = True

    worker = NotifierWorker(
        queue=queue, db_engine=db_engine,
        transports={"email": transport},
    )
    queue.enqueue(f"{sid}:{eid}", _build_job(sid, eid))
    worker.run_once_for_test(max_jobs=1)

    assert worker.jobs_succeeded == 0
    assert worker.jobs_failed == 1
    # Dedup slot must be cleared so the next distributor cycle can re-enqueue.
    assert queue.in_flight_count() == 0


def test_failed_send_allows_immediate_reenqueue(db_engine, queue, seeded):
    """A2 invariant: after a failed send + nack, the same (sub, event)
    can be enqueued again right away."""
    sid, eid, _ = seeded
    transport = RecordingTransport()
    transport.should_fail = True

    worker = NotifierWorker(
        queue=queue, db_engine=db_engine,
        transports={"email": transport},
    )
    queue.enqueue(f"{sid}:{eid}", _build_job(sid, eid))
    worker.run_once_for_test(max_jobs=1)

    # Distributor's next cycle: re-enqueue should succeed.
    assert queue.enqueue(f"{sid}:{eid}", _build_job(sid, eid)) is True


def test_failed_send_writes_no_log_row(db_engine, queue, seeded):
    sid, eid, _ = seeded
    transport = RecordingTransport()
    transport.should_fail = True
    worker = NotifierWorker(
        queue=queue, db_engine=db_engine,
        transports={"email": transport},
    )
    queue.enqueue(f"{sid}:{eid}", _build_job(sid, eid))
    worker.run_once_for_test(max_jobs=1)

    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT count(*) FROM notification_log "
            "WHERE subscriber_id = :sid AND event_id = :eid"
        ), {"sid": sid, "eid": eid}).scalar()
    assert rows == 0


# ---------- Bad payload ---------- #


def test_malformed_payload_is_counted_as_failed(db_engine, queue):
    """A job with missing fields should not crash the worker — counted
    as failed, slot stays held."""
    worker = NotifierWorker(
        queue=queue, db_engine=db_engine,
        transports={"email": RecordingTransport()},
    )
    queue.enqueue("bad-1", {"_job_id": "bad-1", "garbage": "yes"})

    processed = worker.run_once_for_test(max_jobs=1)
    assert processed == 1
    assert worker.jobs_failed == 1


# ---------- Unknown transport falls back ---------- #


def test_unknown_transport_uses_null_returning_success(db_engine, queue, seeded):
    """If the subscriber's transport isn't in our map, fall back to
    NullTransport, which always returns True. The job is ack'd."""
    sid, eid, _ = seeded
    # Build a job with an unknown transport name
    job = _build_job(sid, eid)
    job["transport"] = "carrier-pigeon"
    queue.enqueue(f"{sid}:{eid}", job)

    worker = NotifierWorker(
        queue=queue, db_engine=db_engine,
        transports={"email": RecordingTransport()},
    )
    worker.run_once_for_test(max_jobs=1)
    # NullTransport returns True so it's a "success" — log row written
    assert worker.jobs_succeeded == 1
    assert queue.in_flight_count() == 0


# ---------- Multiple jobs in sequence ---------- #


def test_processes_multiple_jobs_in_order(db_engine, queue, now):
    """Two events, two jobs — both should be processed."""
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(NIFTY, created_at=now)
        signals = SignalsRepository(conn)
        eid1 = signals.insert_event(tid, SignalEvent(
            ticker=NIFTY, timeframe=Timeframe.WEEKLY,
            bar_timestamp=now - timedelta(hours=2),
            signal=ChangeSignal.BUY, price_at_signal=24000,
            computed_at=now - timedelta(hours=2),
        ))
        eid2 = signals.insert_event(tid, SignalEvent(
            ticker=NIFTY, timeframe=Timeframe.WEEKLY,
            bar_timestamp=now - timedelta(hours=1),
            signal=ChangeSignal.SELL, price_at_signal=23800,
            computed_at=now - timedelta(hours=1),
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

    transport = RecordingTransport()
    worker = NotifierWorker(
        queue=queue, db_engine=db_engine,
        transports={"email": transport},
    )
    queue.enqueue(f"{sid}:{eid1}", _build_job(sid, eid1))
    queue.enqueue(f"{sid}:{eid2}", _build_job(sid, eid2))

    worker.run_once_for_test(max_jobs=2)
    assert worker.jobs_succeeded == 2
    assert len(transport.calls) == 2
    assert queue.in_flight_count() == 0


# ---------- Worker invariants ---------- #


def test_worker_count_below_one_rejected(db_engine, queue):
    with pytest.raises(ValueError):
        NotifierWorker(
            queue=queue, db_engine=db_engine,
            transports={}, worker_count=0,
        )


def test_run_once_for_test_returns_zero_on_empty_queue(db_engine, queue):
    worker = NotifierWorker(
        queue=queue, db_engine=db_engine, transports={},
    )
    assert worker.run_once_for_test(max_jobs=5) == 0
