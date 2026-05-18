"""Phase 12 — DLQ behavior at the worker level.

When the transport fails, the worker increments a persistent
attempts counter. Once it crosses max_attempts the worker moves the
job to the DLQ instead of nacking. This catches the pathology where
a circuit-broken transport would otherwise cycle the same job
forever (distributor re-enqueues every cycle, worker fails every
cycle).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock

import fakeredis
import pytest

from trip_ema.domain import NotificationBatch
from trip_ema.notifications.base import NotificationTransport
from trip_ema.notifications.redis_queue import RedisQueue
from trip_ema.notifier_worker import NotifierWorker


# -------------------- Test doubles -------------------- #


class FailingTransport(NotificationTransport):
    """Always returns False from send()."""
    def __init__(self):
        self.send_count = 0

    @property
    def name(self) -> str:
        return "failing"

    def send(self, address: str, batch: NotificationBatch) -> bool:
        self.send_count += 1
        return False


class FakeDBEngine:
    @contextmanager
    def connect(self):
        yield MagicMock(name="FakeConnection")


class FakeSignalsRepo:
    """Always reports the event as NOT superseded — so the worker
    proceeds to call the transport (which fails)."""
    def __init__(self, conn):
        pass

    def is_event_superseded(self, *args, **kwargs):
        return False


class FakeSubscribersRepo:
    def __init__(self, conn):
        pass

    def record_notification(self, **kwargs):
        pass

    def update_last_notified(self, *args, **kwargs):
        pass


# -------------------- Fixtures -------------------- #


@pytest.fixture
def redis_queue():
    fake = fakeredis.FakeStrictRedis(decode_responses=True, server_type="redis")
    return RedisQueue(
        redis_client=fake,
        queue_name="test_q",
        dedup_hash_name="test_dedup",
    )


@pytest.fixture
def failing_transport():
    return FailingTransport()


@pytest.fixture
def fake_db_engine():
    return FakeDBEngine()


@pytest.fixture
def base_job():
    return {
        "subscriber_id": 42,
        "transport": "failing",
        "address": "user@example.com",
        "event_id": 1001,
        "event": {
            "ticker": {"exchange": "NSE", "symbol": "NIFTY", "instrument": "INDEX"},
            "timeframe": "1W",
            "bar_timestamp": "2026-05-04T00:00:00+00:00",
            "signal": "BUY",
            "price_at_signal": 22000.0,
            "computed_at": "2026-05-04T00:01:00+00:00",
        },
        "enqueued_at": "2026-05-04T00:02:00+00:00",
    }


@pytest.fixture
def patch_repos(monkeypatch):
    """Patch the worker's DB-touching imports to fakes."""
    monkeypatch.setattr(
        "trip_ema.notifier_worker.SignalsRepository", FakeSignalsRepo,
    )
    monkeypatch.setattr(
        "trip_ema.notifier_worker.SubscribersRepository", FakeSubscribersRepo,
    )

    @contextmanager
    def fake_transaction(engine):
        yield MagicMock(name="FakeTxnConn")

    monkeypatch.setattr(
        "trip_ema.notifier_worker.transaction", fake_transaction,
    )


def _make_worker(redis_queue, transport, fake_db_engine, max_attempts=3):
    return NotifierWorker(
        queue=redis_queue,
        db_engine=fake_db_engine,
        transports={"failing": transport},
        max_attempts=max_attempts,
    )


# -------------------- Tests -------------------- #


class TestDlqRetryThreshold:
    """The worker moves a job to DLQ once max_attempts is reached."""

    def test_first_failure_nacks_not_dlq(
        self, patch_repos, redis_queue, failing_transport,
        fake_db_engine, base_job,
    ):
        worker = _make_worker(
            redis_queue, failing_transport, fake_db_engine, max_attempts=3,
        )
        redis_queue.enqueue("job-1", base_job)
        job = redis_queue.pop_blocking(timeout_seconds=1)
        worker._handle_job(job)

        assert failing_transport.send_count == 1
        assert worker.jobs_failed == 1
        assert worker.jobs_moved_to_dlq == 0
        assert redis_queue.dlq_depth() == 0
        # nack freed the dedup slot
        assert redis_queue.in_flight_count() == 0
        # Persistent attempt counter is now 1
        assert redis_queue.get_attempts("job-1") == 1

    def test_failures_accumulate_across_enqueues(
        self, patch_repos, redis_queue, failing_transport,
        fake_db_engine, base_job,
    ):
        """The attempts counter survives nack + re-enqueue (the
        critical property that the pre-Phase-12 design lacked)."""
        worker = _make_worker(
            redis_queue, failing_transport, fake_db_engine, max_attempts=5,
        )
        # Simulate the distributor's cycle: enqueue, fail, nack, repeat.
        for i in range(3):
            redis_queue.enqueue("job-1", base_job)
            job = redis_queue.pop_blocking(timeout_seconds=1)
            worker._handle_job(job)

        assert redis_queue.get_attempts("job-1") == 3
        assert redis_queue.dlq_depth() == 0  # not yet at max

    def test_max_attempts_triggers_dlq_move(
        self, patch_repos, redis_queue, failing_transport,
        fake_db_engine, base_job,
    ):
        worker = _make_worker(
            redis_queue, failing_transport, fake_db_engine, max_attempts=3,
        )
        # Three failure cycles
        for _ in range(3):
            redis_queue.enqueue("job-1", base_job)
            job = redis_queue.pop_blocking(timeout_seconds=1)
            worker._handle_job(job)

        assert worker.jobs_moved_to_dlq == 1
        assert redis_queue.dlq_depth() == 1
        # Attempts counter cleared by move_to_dlq
        assert redis_queue.get_attempts("job-1") == 0
        # Dedup slot freed
        assert redis_queue.in_flight_count() == 0

    def test_dlq_payload_carries_metadata(
        self, patch_repos, redis_queue, failing_transport,
        fake_db_engine, base_job,
    ):
        worker = _make_worker(
            redis_queue, failing_transport, fake_db_engine, max_attempts=2,
        )
        for _ in range(2):
            redis_queue.enqueue("job-1", base_job)
            job = redis_queue.pop_blocking(timeout_seconds=1)
            worker._handle_job(job)

        entries = redis_queue.list_dlq()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["_job_id"] == "job-1"
        assert entry["_attempts"] == 2
        assert "max_attempts (2) exceeded" in entry["_dlq_reason"]
        assert "_dlq_at" in entry
        # Original payload fields preserved
        assert entry["subscriber_id"] == 42
        assert entry["event_id"] == 1001

    def test_successful_send_clears_attempts(
        self, patch_repos, redis_queue, fake_db_engine, base_job,
    ):
        """If a job fails twice then succeeds on the third attempt,
        the attempts counter resets — the next failure (if any)
        starts from 1, not from 3."""
        # Use a transport that fails the first two times then succeeds
        class FlakeyTransport(NotificationTransport):
            def __init__(self):
                self.send_count = 0
            @property
            def name(self):
                return "failing"
            def send(self, address, batch):
                self.send_count += 1
                return self.send_count >= 3

        flakey = FlakeyTransport()
        worker = NotifierWorker(
            queue=redis_queue,
            db_engine=fake_db_engine,
            transports={"failing": flakey},
            max_attempts=5,
        )
        # First failure
        redis_queue.enqueue("job-1", base_job)
        worker._handle_job(redis_queue.pop_blocking(timeout_seconds=1))
        assert redis_queue.get_attempts("job-1") == 1
        # Second failure
        redis_queue.enqueue("job-1", base_job)
        worker._handle_job(redis_queue.pop_blocking(timeout_seconds=1))
        assert redis_queue.get_attempts("job-1") == 2
        # Third attempt succeeds
        redis_queue.enqueue("job-1", base_job)
        worker._handle_job(redis_queue.pop_blocking(timeout_seconds=1))

        assert worker.jobs_succeeded == 1
        assert redis_queue.get_attempts("job-1") == 0
        assert redis_queue.dlq_depth() == 0
