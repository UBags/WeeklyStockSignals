"""End-to-end test that the observability instrumentation in
ComputeScheduler.run_once() actually fires the Prometheus metrics.

Re-uses the FakeBarsRepo / FakeRegistry / FakeDBEngine doubles from
the gap-detection tests so we don't need Postgres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from trip_ema.cache import SignalCache
from trip_ema.domain import (
    IndicatorParams,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from trip_ema.engine import TickerEngine
from trip_ema.observability import metrics as M

# Re-use the harness from the gap-detection tests
from tests.scheduler_pure.test_gap_detection import (
    FakeBarsRepo,
    FakeDBEngine,
    FakeRegistry,
    _build_primed_engine,
    _seed_cache_for_engine,
)


def _get_counter_value(counter, **labels) -> float:
    """Read a labeled counter's current value."""
    if labels:
        return counter.labels(**labels)._value.get()
    return counter._value.get()


class TestSchedulerInstrumentation:

    UTC = timezone.utc

    def test_run_once_increments_runs_completed_counter(self, monkeypatch):
        """A normal run_once() must bump scheduler_runs_total{outcome='completed'}."""
        from trip_ema.scheduler.compute_scheduler import ComputeScheduler

        last_monday = datetime(2026, 5, 4, tzinfo=self.UTC)
        now = datetime(2026, 5, 19, 10, 0, tzinfo=self.UTC)
        ticker = TickerRef("NSE", "NIFTY", "INDEX")
        engine = _build_primed_engine(last_monday=last_monday)
        cache = SignalCache()
        _seed_cache_for_engine(cache, engine, now=now - timedelta(hours=1))
        bars_repo = FakeBarsRepo(
            source_bars={(1, "1D"): [
                OHLCVBar(
                    timestamp=datetime(2026, 5, 11 + i, tzinfo=self.UTC),
                    open=100.0, high=101.0, low=99.0,
                    close=100.5, volume=1000.0,
                )
                for i in range(5)
            ] + [
                OHLCVBar(
                    timestamp=datetime(2026, 5, 18 + i, tzinfo=self.UTC),
                    open=100.0, high=101.0, low=99.0,
                    close=100.5, volume=1000.0,
                )
                for i in range(2)
            ]},
        )
        registry = FakeRegistry(
            ticker_id=1, ref=ticker, engine=engine,
            tz="UTC", source_timeframe="1D",
        )
        db_engine = FakeDBEngine()

        monkeypatch.setattr(
            "trip_ema.scheduler.compute_scheduler.BarsRepository",
            lambda conn: bars_repo,
        )

        sched = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
            cadence_minutes=60,
        )
        monkeypatch.setattr(sched, "_maybe_extend_partitions", lambda now: None)
        import trip_ema.scheduler.compute_scheduler as sched_mod
        orig_dt = sched_mod.datetime
        class FakeDT(orig_dt):
            @classmethod
            def now(cls, tz=None):
                return now
        monkeypatch.setattr(sched_mod, "datetime", FakeDT)

        before_runs = _get_counter_value(M.scheduler_runs_total, outcome="completed")
        before_examined = _get_counter_value(M.scheduler_tickers_examined_total)

        sched.run_once()

        # Run counter incremented
        after_runs = _get_counter_value(M.scheduler_runs_total, outcome="completed")
        assert after_runs == before_runs + 1.0

        # Examined-tickers counter incremented by 1 (we registered 1)
        after_examined = _get_counter_value(M.scheduler_tickers_examined_total)
        assert after_examined == before_examined + 1.0

    def test_run_once_with_gap_sets_stale_gauge(self, monkeypatch):
        """A run that detects a gap must set the stale_tickers gauge to 1."""
        from trip_ema.scheduler.compute_scheduler import ComputeScheduler

        last_monday = datetime(2026, 5, 4, tzinfo=self.UTC)
        now = datetime(2026, 5, 26, 10, 0, tzinfo=self.UTC)
        ticker = TickerRef("NSE", "NIFTY", "INDEX")
        engine = _build_primed_engine(last_monday=last_monday)
        cache = SignalCache()
        _seed_cache_for_engine(cache, engine, now=now - timedelta(hours=1))

        # Only week of 5/11 has bars; 5/18 missing -> gap
        bars = [
            OHLCVBar(
                timestamp=datetime(2026, 5, 11 + i, tzinfo=self.UTC),
                open=100.0, high=101.0, low=99.0,
                close=100.5, volume=1000.0,
            )
            for i in range(5)
        ]
        bars_repo = FakeBarsRepo(source_bars={(1, "1D"): bars})
        registry = FakeRegistry(
            ticker_id=1, ref=ticker, engine=engine,
            tz="UTC", source_timeframe="1D",
        )

        monkeypatch.setattr(
            "trip_ema.scheduler.compute_scheduler.BarsRepository",
            lambda conn: bars_repo,
        )

        sched = ComputeScheduler(
            db_engine=FakeDBEngine(), registry=registry, cache=cache,
            cadence_minutes=60,
        )
        monkeypatch.setattr(sched, "_maybe_extend_partitions", lambda now: None)
        import trip_ema.scheduler.compute_scheduler as sched_mod
        orig_dt = sched_mod.datetime
        class FakeDT(orig_dt):
            @classmethod
            def now(cls, tz=None):
                return now
        monkeypatch.setattr(sched_mod, "datetime", FakeDT)

        sched.run_once()

        # The gauge should reflect 1 stale ticker
        assert M.scheduler_stale_tickers._value.get() == 1.0
        # Cache_size gauge reflects the lone entry
        assert M.cache_size._value.get() == 1.0

    def test_concurrent_run_increments_skipped_counter(self, monkeypatch):
        """If run_once is called while another is in flight, it must
        bump scheduler_runs_total{outcome='skipped_concurrent'} and
        return immediately."""
        from trip_ema.scheduler.compute_scheduler import ComputeScheduler

        ticker = TickerRef("NSE", "X", "EQ")
        engine = _build_primed_engine(
            last_monday=datetime(2026, 5, 4, tzinfo=self.UTC),
        )
        cache = SignalCache()
        bars_repo = FakeBarsRepo(source_bars={})
        registry = FakeRegistry(
            ticker_id=1, ref=ticker, engine=engine,
            tz="UTC", source_timeframe="1D",
        )

        sched = ComputeScheduler(
            db_engine=FakeDBEngine(), registry=registry, cache=cache,
            cadence_minutes=60,
        )

        # Hold the lock so the next run_once will skip
        sched._run_lock.acquire()
        try:
            before = _get_counter_value(
                M.scheduler_runs_total, outcome="skipped_concurrent",
            )
            summary = sched.run_once()
            after = _get_counter_value(
                M.scheduler_runs_total, outcome="skipped_concurrent",
            )
            assert summary.skipped_due_to_concurrent_run is True
            assert after == before + 1.0
        finally:
            sched._run_lock.release()


class TestNotifierInstrumentation:
    """The notifier worker's _handle_job is already tested in
    tests/notifier_pure; here we just verify that the metrics fire
    when a job is processed."""

    def test_sent_outcome_increments_metric(self, monkeypatch):
        from contextlib import contextmanager
        from unittest.mock import MagicMock
        import fakeredis
        from trip_ema.notifications.base import NotificationTransport
        from trip_ema.notifications.redis_queue import RedisQueue
        from trip_ema.notifier_worker import NotifierWorker

        class Recording(NotificationTransport):
            @property
            def name(self): return "recording"
            def send(self, address, batch):
                return True

        class FreshSignalsRepo:
            def __init__(self, conn): pass
            def is_event_superseded(self, event_id): return False

        class FakeSubsRepo:
            def __init__(self, conn): pass
            def record_notification(self, **kwargs): pass
            def update_last_notified(self, *a, **kw): pass

        @contextmanager
        def fake_txn(engine):
            yield MagicMock()

        monkeypatch.setattr(
            "trip_ema.notifier_worker.SignalsRepository", FreshSignalsRepo,
        )
        monkeypatch.setattr(
            "trip_ema.notifier_worker.SubscribersRepository", FakeSubsRepo,
        )
        monkeypatch.setattr("trip_ema.notifier_worker.transaction", fake_txn)

        class FakeDBEng:
            @contextmanager
            def connect(self):
                yield MagicMock()

        redis_client = fakeredis.FakeStrictRedis(decode_responses=True, server_type="redis")
        queue = RedisQueue(
            redis_client=redis_client, queue_name="qx", dedup_hash_name="dx",
        )
        worker = NotifierWorker(
            queue=queue, db_engine=FakeDBEng(),
            transports={"recording": Recording()},
        )
        payload = {
            "subscriber_id": 1, "transport": "recording",
            "address": "x@y.z", "event_id": 999,
            "correlation_id": "test-cid-abc",
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
        queue.enqueue("job-metric-1", payload)

        before_sent = _get_counter_value(
            M.notifier_jobs_processed_total,
            outcome="sent", transport="recording",
        )
        worker.run_once_for_test()
        after_sent = _get_counter_value(
            M.notifier_jobs_processed_total,
            outcome="sent", transport="recording",
        )
        assert after_sent == before_sent + 1.0
