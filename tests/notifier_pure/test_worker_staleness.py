"""Phase 9 NEW 3.4 — Worker-side staleness check tests.

When the notifier worker is about to dispatch a job, it must check the
DB for a newer signal_event for the same (ticker, timeframe) and drop
the job (ack without sending) if one exists. This prevents stale BUY
alerts arriving after an EXIT was already generated.

These tests use a fake db_engine + a patched SignalsRepository to
exercise the worker logic without needing a real Postgres.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock

import fakeredis
import pytest

from trip_ema.domain import ChangeSignal, NotificationBatch, TickerRef, Timeframe
from trip_ema.notifications.redis_queue import RedisQueue
from trip_ema.notifications.base import NotificationTransport
from trip_ema.notifier_worker import NotifierWorker


# -------------------- Test doubles -------------------- #


class RecordingTransport(NotificationTransport):
    """Captures send() calls."""
    def __init__(self, return_value: bool = True):
        self.sent: list[tuple[str, NotificationBatch]] = []
        self._rv = return_value

    @property
    def name(self) -> str:
        return "recording"

    def send(self, address: str, batch: NotificationBatch) -> bool:
        self.sent.append((address, batch))
        return self._rv


class FakeDBEngine:
    """db_engine substitute. The worker's only DB calls go through
    `connect()` and `transaction()`. We yield MagicMock connections;
    the SignalsRepository / SubscribersRepository classes are patched
    at the import site."""

    @contextmanager
    def connect(self):
        yield MagicMock(name="FakeConnection")

    # `transaction()` is a context manager IMPORTED into the worker
    # module — we patch it separately.


class FakeSubscribersRepo:
    """Captures notification-log writes."""
    def __init__(self, conn):
        self.records: list[dict] = []
        self.last_notified_updates: list[tuple[int, datetime]] = []

    def record_notification(self, *, subscriber_id, event_id, sent_at):
        self.records.append({
            "subscriber_id": subscriber_id,
            "event_id": event_id,
            "sent_at": sent_at,
        })

    def update_last_notified(self, subscriber_id, at):
        self.last_notified_updates.append((subscriber_id, at))


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
def transport():
    return RecordingTransport(return_value=True)


@pytest.fixture
def fake_db_engine():
    return FakeDBEngine()


@pytest.fixture
def base_job():
    """A canned signal_event payload matching what the distributor enqueues."""
    return {
        "subscriber_id": 42,
        "transport": "recording",
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


def _make_worker(redis_queue, transport, fake_db_engine):
    worker = NotifierWorker(
        queue=redis_queue,
        db_engine=fake_db_engine,
        transports={"recording": transport},
    )
    return worker


# -------------------- Tests -------------------- #


class TestWorkerStalenessCheck:
    """The worker must drop superseded jobs without calling transport
    and without writing notification_log."""

    def test_superseded_event_is_dropped_without_sending(
        self, monkeypatch, redis_queue, transport, fake_db_engine, base_job,
    ):
        """When SignalsRepository.is_event_superseded returns True, the
        worker must NOT call transport.send and must NOT write to
        notification_log. The job is ack'd (jobs_dropped_stale += 1)."""
        # Patch SignalsRepository at the worker import site
        class FakeSignalsRepoSuperseded:
            def __init__(self, conn):
                pass
            def is_event_superseded(self, event_id):
                return True
        monkeypatch.setattr(
            "trip_ema.notifier_worker.SignalsRepository",
            FakeSignalsRepoSuperseded,
        )
        # The transaction context wouldn't be hit — but patch defensively
        @contextmanager
        def fake_transaction(engine):
            yield MagicMock()
        monkeypatch.setattr("trip_ema.notifier_worker.transaction", fake_transaction)

        worker = _make_worker(redis_queue, transport, fake_db_engine)

        # Enqueue a job
        job_id = "job-superseded-1"
        redis_queue.enqueue(job_id, base_job)
        # Worker pops and processes one
        processed = worker.run_once_for_test()
        assert processed == 1

        # Transport NOT called
        assert transport.sent == []
        # Counters: 1 dropped stale, 0 sent, 0 failed
        assert worker.jobs_dropped_stale == 1
        assert worker.jobs_succeeded == 0
        assert worker.jobs_failed == 0
        # Slot freed (ack happened) — re-enqueue with same job_id should succeed
        assert redis_queue.enqueue(job_id, base_job) is True

    def test_fresh_event_proceeds_normally(
        self, monkeypatch, redis_queue, transport, fake_db_engine, base_job,
    ):
        """When is_event_superseded returns False, the normal send path
        runs: transport.send is called, notification_log is written."""
        class FakeSignalsRepoFresh:
            def __init__(self, conn):
                pass
            def is_event_superseded(self, event_id):
                return False
        monkeypatch.setattr(
            "trip_ema.notifier_worker.SignalsRepository",
            FakeSignalsRepoFresh,
        )
        # Stub transaction + SubscribersRepository so log-write doesn't blow up
        sub_repo_holder = {}
        @contextmanager
        def fake_transaction(engine):
            conn = MagicMock(name="TxnConn")
            yield conn
        monkeypatch.setattr("trip_ema.notifier_worker.transaction", fake_transaction)
        # SubscribersRepository is instantiated inside the transaction —
        # replace it with our fake.
        repo_calls = []
        def fake_subs_repo_factory(conn):
            repo = FakeSubscribersRepo(conn)
            repo_calls.append(repo)
            return repo
        monkeypatch.setattr(
            "trip_ema.notifier_worker.SubscribersRepository",
            fake_subs_repo_factory,
        )

        worker = _make_worker(redis_queue, transport, fake_db_engine)
        redis_queue.enqueue("job-fresh-1", base_job)
        processed = worker.run_once_for_test()
        assert processed == 1

        # Transport called once
        assert len(transport.sent) == 1
        address, batch = transport.sent[0]
        assert address == "user@example.com"
        assert batch.subscriber_id == 42
        # Counters
        assert worker.jobs_succeeded == 1
        assert worker.jobs_dropped_stale == 0
        assert worker.jobs_failed == 0
        # notification_log written
        assert len(repo_calls) == 1
        assert repo_calls[0].records == [
            {"subscriber_id": 42, "event_id": 1001, "sent_at": repo_calls[0].records[0]["sent_at"]},
        ]

    def test_transport_failure_does_not_consult_staleness(
        self, monkeypatch, redis_queue, fake_db_engine, base_job,
    ):
        """If is_event_superseded returns False but transport.send fails,
        the job nacks (jobs_failed += 1), NOT drops_stale."""
        class FakeSignalsRepoFresh:
            def __init__(self, conn):
                pass
            def is_event_superseded(self, event_id):
                return False
        monkeypatch.setattr(
            "trip_ema.notifier_worker.SignalsRepository",
            FakeSignalsRepoFresh,
        )
        # Transport that fails
        bad_transport = RecordingTransport(return_value=False)

        worker = NotifierWorker(
            queue=redis_queue,
            db_engine=fake_db_engine,
            transports={"recording": bad_transport},
        )
        redis_queue.enqueue("job-fail-1", base_job)
        processed = worker.run_once_for_test()
        assert processed == 1

        # Transport called (and returned False)
        assert len(bad_transport.sent) == 1
        # Counters: 1 failed, 0 dropped, 0 succeeded
        assert worker.jobs_failed == 1
        assert worker.jobs_dropped_stale == 0
        assert worker.jobs_succeeded == 0

    def test_supersede_check_happens_before_transport_call(
        self, monkeypatch, redis_queue, fake_db_engine, base_job,
    ):
        """Verify ordering: the staleness check happens BEFORE
        transport.send. If the check decides superseded, send must not
        be called even once."""
        # A transport that records call order
        transport_calls = []
        class OrderedTransport(NotificationTransport):
            @property
            def name(self):
                return "ordered"
            def send(self, address, batch):
                transport_calls.append(("send", address))
                return True

        check_calls = []
        class CheckingRepo:
            def __init__(self, conn):
                pass
            def is_event_superseded(self, event_id):
                check_calls.append(("check", event_id))
                return True
        monkeypatch.setattr(
            "trip_ema.notifier_worker.SignalsRepository", CheckingRepo,
        )

        worker = NotifierWorker(
            queue=redis_queue, db_engine=fake_db_engine,
            transports={"recording": OrderedTransport()},
        )
        redis_queue.enqueue("job-order-1", base_job)
        worker.run_once_for_test()

        assert check_calls == [("check", 1001)]
        assert transport_calls == []   # send was never called
        assert worker.jobs_dropped_stale == 1
