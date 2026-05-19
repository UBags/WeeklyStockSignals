"""Tests for PulseState — the orchestrator pub/sub.

Covers:
  * Ring buffer semantics (bounded, FIFO eviction, snapshot returns a copy)
  * service_started_at captured at construction
  * publish() works from any thread (no event loop bound)
  * Async fan-out: subscribers see live events
  * Backfill: a new subscriber sees the ring buffer first
  * Backpressure: slow subscriber → drop, not block
  * Service-level events (orchestrator_started/stopped)
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from trip_ema.scheduler.market_orchestrator import (
    OrchestratorRunSummary,
    OrchestratorStepResult,
)
from trip_ema.transports.http.pulse_state import PulseEvent, PulseState


UTC = timezone.utc


def _dummy_summary(exchange: str = "NSE") -> OrchestratorRunSummary:
    s = OrchestratorRunSummary(
        exchange=exchange,
        started_at=datetime(2026, 5, 19, 5, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 19, 5, 0, 1, tzinfo=UTC),
        correlation_id="cid-abc",
    )
    s.fetch.succeeded = True
    s.compute.succeeded = True
    s.distribute.succeeded = True
    return s


# ---------------------------------------------------------------------------
# Construction: service_started_at and the seeded service_started event
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_service_started_at_captured(self):
        now = datetime(2026, 5, 19, 9, 0, 0, tzinfo=UTC)
        ps = PulseState(now_fn=lambda: now)
        assert ps.service_started_at == now

    def test_service_started_event_seeded_into_ring_buffer(self):
        now = datetime(2026, 5, 19, 9, 0, 0, tzinfo=UTC)
        ps = PulseState(now_fn=lambda: now)
        snap = ps.snapshot()
        assert len(snap) == 1
        assert snap[0].event_type == "service_started"
        assert snap[0].timestamp == now
        # GUI clients can use this to detect "the service was just restarted".
        assert snap[0].details == {"pulse_state_initialized": True}

    def test_counters_start_at_zero(self):
        ps = PulseState()
        # The seed service_started event uses the internal _record_event
        # path, NOT publish(), so published_events_total stays 0.
        assert ps.published_events_total == 0
        assert ps.dropped_events_total == 0
        assert ps.subscriber_count == 0


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------


class TestRingBuffer:
    def test_publish_appends_to_ring_buffer(self):
        ps = PulseState()
        s = _dummy_summary()
        ps.publish(s)
        snap = ps.snapshot()
        # First event is service_started (seeded); second is our publish.
        assert len(snap) == 2
        assert snap[-1].event_type == "orchestrator_run"
        assert snap[-1].exchange == "NSE"
        assert snap[-1].summary is s

    def test_ring_buffer_bounded(self):
        ps = PulseState(ring_buffer_size=3)
        # The seed service_started event already consumes 1 slot.
        for i in range(5):
            ps.publish(_dummy_summary(f"EX{i}"))
        snap = ps.snapshot()
        # Only the last 3 events survive.
        assert len(snap) == 3
        assert [e.exchange for e in snap] == ["EX2", "EX3", "EX4"]
        # The original service_started event was evicted.
        assert all(e.event_type != "service_started" for e in snap)

    def test_snapshot_returns_a_copy(self):
        ps = PulseState()
        ps.publish(_dummy_summary())
        snap1 = ps.snapshot()
        ps.publish(_dummy_summary("BSE"))
        # The first snapshot was a copy, so it doesn't see the new event.
        assert len(snap1) == 2
        assert all(e.event_type != "orchestrator_run" or e.exchange == "NSE"
                   for e in snap1)

    def test_published_counter_increments(self):
        ps = PulseState()
        assert ps.published_events_total == 0
        ps.publish(_dummy_summary())
        ps.publish(_dummy_summary())
        assert ps.published_events_total == 2


# ---------------------------------------------------------------------------
# Publish from any thread without a bound loop
# ---------------------------------------------------------------------------


class TestPublishThreadSafety:
    def test_publish_from_worker_thread_with_no_loop_bound(self):
        ps = PulseState()

        def worker():
            for i in range(5):
                ps.publish(_dummy_summary(f"X{i}"))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 3 threads × 5 publishes = 15 events, plus 1 service_started.
        # Ring buffer holds the most recent 50 (default), so all visible.
        snap = ps.snapshot()
        assert ps.published_events_total == 15
        # All published events are present.
        published = [e for e in snap if e.event_type == "orchestrator_run"]
        assert len(published) == 15

    def test_publish_never_raises_even_on_exotic_input(self):
        ps = PulseState()
        # Even with a fully-default empty-ish summary, publish must work.
        s = OrchestratorRunSummary(
            exchange="X", started_at=datetime.now(timezone.utc),
        )
        # Doesn't raise.
        ps.publish(s)


# ---------------------------------------------------------------------------
# Service-level events
# ---------------------------------------------------------------------------


class TestServiceLevelEvents:
    def test_orchestrator_started_event(self):
        ps = PulseState()
        ps.publish_orchestrator_started("NSE")
        snap = ps.snapshot()
        last = snap[-1]
        assert last.event_type == "orchestrator_started"
        assert last.exchange == "NSE"
        assert last.summary is None

    def test_orchestrator_stopped_event_with_reason(self):
        ps = PulseState()
        ps.publish_orchestrator_stopped("NSE", reason="crash")
        snap = ps.snapshot()
        last = snap[-1]
        assert last.event_type == "orchestrator_stopped"
        assert last.exchange == "NSE"
        assert last.details == {"reason": "crash"}

    def test_orchestrator_stopped_default_reason(self):
        ps = PulseState()
        ps.publish_orchestrator_stopped("NSE")
        last = ps.snapshot()[-1]
        assert last.details == {"reason": "normal"}


# ---------------------------------------------------------------------------
# Async subscribers
# ---------------------------------------------------------------------------


class TestAsyncSubscribers:
    """Async-event tests. These exercise the cross-thread queue bridge."""

    @pytest.mark.asyncio
    async def test_subscriber_receives_backfill(self):
        ps = PulseState()
        ps.bind_loop(asyncio.get_running_loop())
        # Pre-populate with one publish.
        ps.publish(_dummy_summary("NSE"))

        # Subscribe. Should see service_started + the NSE publish.
        seen = []
        agen = ps.subscribe()
        # Drain only the backfill events (we know there are exactly 2).
        for _ in range(2):
            seen.append(await agen.__anext__())
        await agen.aclose()
        assert [e.event_type for e in seen] == [
            "service_started", "orchestrator_run",
        ]

    @pytest.mark.asyncio
    async def test_subscriber_receives_live_events(self):
        ps = PulseState()
        ps.bind_loop(asyncio.get_running_loop())

        seen = []
        async def subscriber():
            agen = ps.subscribe()
            # Drain the 1 backfill event (service_started).
            await agen.__anext__()
            # Now wait for live events.
            for _ in range(3):
                seen.append(await agen.__anext__())
            await agen.aclose()

        sub_task = asyncio.create_task(subscriber())
        # Yield control so the subscriber registers.
        await asyncio.sleep(0.01)
        # Publish three events from this same loop's thread; the
        # call_soon_threadsafe path still works (the loop is running).
        for ex in ["A", "B", "C"]:
            ps.publish(_dummy_summary(ex))
        await asyncio.wait_for(sub_task, timeout=2.0)
        assert [e.exchange for e in seen] == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_subscriber_receives_publishes_from_worker_thread(self):
        ps = PulseState()
        ps.bind_loop(asyncio.get_running_loop())

        seen = []
        async def subscriber():
            agen = ps.subscribe()
            await agen.__anext__()  # drain service_started
            seen.append(await agen.__anext__())
            await agen.aclose()

        sub_task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.01)

        # Publish from a real worker thread, not the event loop.
        def worker():
            ps.publish(_dummy_summary("FROM-THREAD"))
        threading.Thread(target=worker).start()

        await asyncio.wait_for(sub_task, timeout=2.0)
        assert seen[0].exchange == "FROM-THREAD"

    @pytest.mark.asyncio
    async def test_subscriber_count_updates(self):
        ps = PulseState()
        ps.bind_loop(asyncio.get_running_loop())

        assert ps.subscriber_count == 0

        async def quick_sub():
            agen = ps.subscribe()
            # Just consume the backfill, then exit.
            await agen.__anext__()
            # Check count while still subscribed.
            assert ps.subscriber_count == 1
            await agen.aclose()

        await quick_sub()
        # After aclose, the subscriber is unregistered.
        assert ps.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_multiple_subscribers_each_get_all_events(self):
        ps = PulseState()
        ps.bind_loop(asyncio.get_running_loop())

        results: dict[str, list] = {"sub1": [], "sub2": []}

        async def subscriber(name: str):
            agen = ps.subscribe()
            await agen.__anext__()  # drain service_started backfill
            for _ in range(2):
                results[name].append(await agen.__anext__())
            await agen.aclose()

        t1 = asyncio.create_task(subscriber("sub1"))
        t2 = asyncio.create_task(subscriber("sub2"))
        await asyncio.sleep(0.01)  # let both subscribers register

        ps.publish(_dummy_summary("X"))
        ps.publish(_dummy_summary("Y"))

        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)
        assert [e.exchange for e in results["sub1"]] == ["X", "Y"]
        assert [e.exchange for e in results["sub2"]] == ["X", "Y"]


# ---------------------------------------------------------------------------
# Backpressure: slow subscribers get events dropped, not blocked
# ---------------------------------------------------------------------------


class TestBackpressure:
    @pytest.mark.asyncio
    async def test_slow_subscriber_drops_events(self):
        # Small queue so we exhaust it quickly.
        ps = PulseState(subscriber_queue_size=2)
        ps.bind_loop(asyncio.get_running_loop())

        # Manually create a subscriber that never reads from its queue
        # (simulating a stuck SSE client).
        slow_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        with ps._lock:
            ps._subscribers.add(slow_queue)

        # Fire more events than the queue can hold.
        for i in range(10):
            ps.publish(_dummy_summary(f"EX{i}"))

        # Yield to let the call_soon_threadsafe tasks run.
        await asyncio.sleep(0.05)

        # Slow queue is full (2 events). 8 events were dropped.
        # We accept >= 8 since timing can leave the queue with 0..2 items.
        assert slow_queue.qsize() <= 2
        assert ps.dropped_events_total >= 8
        # The publisher's success counter still went up: dropped on the
        # subscriber side, but accepted into the ring buffer.
        assert ps.published_events_total == 10

        # Cleanup.
        with ps._lock:
            ps._subscribers.discard(slow_queue)


# ---------------------------------------------------------------------------
# Loop binding semantics
# ---------------------------------------------------------------------------


class TestLoopBinding:
    @pytest.mark.asyncio
    async def test_bind_loop_idempotent_on_same_loop(self):
        ps = PulseState()
        loop = asyncio.get_running_loop()
        ps.bind_loop(loop)
        ps.bind_loop(loop)  # no-op; must not log a warning
        # (Caplog assertion omitted — focusing on behavior.)
        assert ps._loop is loop  # noqa: access private

    def test_publish_before_loop_bound_still_records(self):
        ps = PulseState()
        assert ps._loop is None  # noqa
        ps.publish(_dummy_summary())
        # The ring buffer has the event even though no loop is bound.
        snap = ps.snapshot()
        assert any(e.event_type == "orchestrator_run" for e in snap)


# ---------------------------------------------------------------------------
# PulseEvent shape
# ---------------------------------------------------------------------------


class TestPulseEvent:
    def test_event_is_frozen(self):
        e = PulseEvent(event_type="x", timestamp=datetime.now(timezone.utc))
        with pytest.raises(Exception):  # FrozenInstanceError on dataclass
            e.event_type = "y"

    def test_default_optional_fields_are_none(self):
        e = PulseEvent(event_type="x", timestamp=datetime.now(timezone.utc))
        assert e.exchange is None
        assert e.summary is None
        assert e.details is None
