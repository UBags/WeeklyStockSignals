"""PulseState — in-memory pub/sub for orchestrator pipeline events.

Concrete implementation of the `PulsePublisher` Protocol declared in
`trip_ema.scheduler.market_orchestrator`. Lives in the HTTP transport
package because that's the only consumer — the SSE endpoint in
`routes/pulse.py` (Step 5) subscribes to this, and the synchronous
snapshot endpoint reads from it too.

Two concerns this module handles, kept separate:

  1. **Ring buffer of recent events.** Bounded (default 50), used by
     the synchronous `/v1/pulse` snapshot endpoint. Readers see the
     most recent N pulses; older events are dropped.

  2. **Async fan-out to live subscribers.** Each SSE client gets its
     own asyncio.Queue. `publish()` walks the subscriber list and
     enqueues a copy of the event for each. Backpressure: a subscriber
     whose queue is full gets the event dropped (we never block the
     publisher). Tracked in `dropped_events_total` for observability.

Critical threading detail
-------------------------
`publish()` is called from the orchestrator's *thread*, not from the
asyncio event loop. The SSE subscribers live on the asyncio event loop.
To bridge correctly, the asyncio loop is captured at PulseState
construction (via `_bind_loop`); subsequent `publish()` calls schedule
queue puts via `loop.call_soon_threadsafe`.

If the loop isn't bound yet (PulseState constructed before app startup),
publishes still record to the ring buffer but the async fan-out is a
no-op. Once the loop binds, future publishes fan out normally. This
keeps the construction-time call sites simple.

A `service_started_at` field is captured at construction. The Step 5
`/v1/pulse` endpoint surfaces this so the GUI can detect service
restarts and decide whether to refresh views from scratch.

What is NOT here
----------------
* The HTTP endpoints themselves (Step 5).
* Any persistence — the ring buffer is in-memory only. A restart
  resets it. That's intentional: pulse is for live observability,
  not historical analysis. Historical data lives in the
  `signal_events` and `etl_runs` tables.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from ...scheduler.market_orchestrator import OrchestratorRunSummary

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public event types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PulseEvent:
    """One pulse event, suitable for both snapshot and stream consumers.

    The frozen+slots dataclass is for cheap immutability — once
    constructed, an event flows to N subscribers without copy concerns.
    """
    event_type: str
    """Discriminator: 'orchestrator_run' | 'service_started' |
    'orchestrator_started' | 'orchestrator_stopped'."""
    timestamp: datetime
    """When the event happened (UTC)."""
    exchange: Optional[str] = None
    """Set for orchestrator-scoped events; None for service-level events."""
    summary: Optional[OrchestratorRunSummary] = None
    """Set only for `orchestrator_run` events. The full run summary so
    GUI clients can render whichever fields they want without further
    HTTP round-trips."""
    details: Optional[dict] = None
    """Free-form payload for service-level events. Kept loose because
    each event type carries different fields and we don't want a
    type explosion."""


# ---------------------------------------------------------------------------
# PulseState
# ---------------------------------------------------------------------------


class PulseState:
    """The pub/sub itself.

    Construct once at app startup. Pass to each MarketOrchestrator as
    its `pulse` argument. Subscribe via `subscribe()` from the SSE
    endpoint. Snapshot via `snapshot()` for the synchronous endpoint.

    Thread-safety:
      * `publish()`, `snapshot()`, `_register_subscriber`,
        `_unregister_subscriber` are safe to call from any thread.
      * `subscribe()` must be called from the asyncio event loop
        (it's an async generator).
      * `_bind_loop()` must be called once from the asyncio event
        loop, before any subscribers connect.
    """

    DEFAULT_RING_BUFFER_SIZE = 50
    """Number of recent events to retain for /v1/pulse snapshot."""

    DEFAULT_SUBSCRIBER_QUEUE_SIZE = 64
    """Per-subscriber queue depth. If a subscriber falls this far behind,
    we drop events for it (the alternative is unbounded memory growth)."""

    def __init__(
        self,
        *,
        ring_buffer_size: int = DEFAULT_RING_BUFFER_SIZE,
        subscriber_queue_size: int = DEFAULT_SUBSCRIBER_QUEUE_SIZE,
        now_fn=lambda: datetime.now(timezone.utc),
    ):
        self._ring_buffer: deque[PulseEvent] = deque(maxlen=ring_buffer_size)
        self._subscriber_queue_size = subscriber_queue_size
        self._now_fn = now_fn

        # Protects _ring_buffer + _subscribers. Coarse but fine —
        # publish() is bursty (one call per orchestrator tick, at most
        # a few times per hour total), not hot.
        self._lock = threading.Lock()

        # Set of registered subscriber queues. Each SSE consumer
        # registers a Queue, polls it, unregisters on disconnect.
        self._subscribers: set[asyncio.Queue[PulseEvent]] = set()

        # The asyncio loop where subscribers live. Bound at startup
        # via _bind_loop; until bound, publish() skips async fan-out.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Captured at construction. Surfaced via the pulse snapshot so
        # GUI clients can detect service restarts.
        self.service_started_at: datetime = self._now_fn()

        # Cheap counters for observability. Read-only from outside.
        self._dropped_events_total: int = 0
        self._published_events_total: int = 0

        # Publish the service_started event into the ring buffer so a
        # client connecting later still sees it.
        self._record_event(PulseEvent(
            event_type="service_started",
            timestamp=self.service_started_at,
            details={"pulse_state_initialized": True},
        ))

    # ---- Lifecycle wiring ----

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the asyncio event loop. Called from the FastAPI startup
        event (Step 5/6), once. After this, publishes fan out to live
        subscribers.

        Idempotent on the same loop. Re-binding to a different loop
        logs a warning and uses the newer one — this can legitimately
        happen during certain test patterns.
        """
        if self._loop is loop:
            return
        if self._loop is not None:
            logger.warning(
                "PulseState.bind_loop called twice with different loops; "
                "using the latest. This is usually a test-setup quirk; "
                "in production the loop should bind once at startup.",
            )
        self._loop = loop

    # ---- PulsePublisher protocol (called from orchestrator thread) ----

    def publish(self, summary: OrchestratorRunSummary) -> None:
        """Emit an orchestrator-run event.

        Called from the orchestrator's worker thread. Never raises:
        the orchestrator wraps this in a try/except, but we belt-and-
        braces here too because pulse failures must never affect the
        pipeline."""
        event = PulseEvent(
            event_type="orchestrator_run",
            timestamp=self._now_fn(),
            exchange=summary.exchange,
            summary=summary,
        )
        self._record_and_fan_out(event)

    # ---- Service-level events (called from app lifecycle code) ----

    def publish_orchestrator_started(self, exchange: str) -> None:
        """Emit when a MarketOrchestrator's loop thread starts."""
        self._record_and_fan_out(PulseEvent(
            event_type="orchestrator_started",
            timestamp=self._now_fn(),
            exchange=exchange,
        ))

    def publish_orchestrator_stopped(
        self, exchange: str, *, reason: str = "normal",
    ) -> None:
        """Emit when a MarketOrchestrator's loop thread stops.

        `reason`: 'normal' (clean shutdown) | 'crash' (thread died
        unexpectedly). Surfaced to the GUI as a banner."""
        self._record_and_fan_out(PulseEvent(
            event_type="orchestrator_stopped",
            timestamp=self._now_fn(),
            exchange=exchange,
            details={"reason": reason},
        ))

    # ---- Synchronous reader (called from HTTP route handlers) ----

    def snapshot(self) -> list[PulseEvent]:
        """Return a list copy of the current ring buffer, most-recent last.

        Safe to call from any thread. Callers should treat the result
        as immutable (frozen dataclass instances)."""
        with self._lock:
            return list(self._ring_buffer)

    @property
    def dropped_events_total(self) -> int:
        return self._dropped_events_total

    @property
    def published_events_total(self) -> int:
        return self._published_events_total

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ---- Async subscription (called from SSE route handler) ----

    def open_subscription(self) -> "Subscription":
        """Register a fresh subscriber synchronously and return a
        Subscription handle.

        This is a synchronous method on purpose: it ensures the
        subscriber queue is registered BEFORE the caller can perform
        any other awaits. If we used an async generator that
        registered on first `__anext__()`, an event publisher
        running before the first iteration would miss this
        subscriber. Splitting registration from iteration
        eliminates that race.

        Use as a context manager so the subscription is cleanly
        unregistered on exit:

            with pulse_state.open_subscription() as sub:
                # `sub` exposes the backfill snapshot synchronously
                # and an async generator for live events.
                async for event in sub.events():
                    ...

        Tests can also use the handle directly without `with`, but
        production code should always use the context manager so
        the queue is removed from `_subscribers` even on exceptions.
        """
        queue: asyncio.Queue[PulseEvent] = asyncio.Queue(
            maxsize=self._subscriber_queue_size,
        )
        # Snapshot the ring buffer at the same moment we register.
        # Together under the lock: no events can be published between
        # snapshotting and registering (which would otherwise mean
        # missing an event, since it wouldn't be in the snapshot AND
        # the subscriber wasn't yet registered to receive it).
        with self._lock:
            backfill = list(self._ring_buffer)
            self._subscribers.add(queue)
        return Subscription(state=self, queue=queue, backfill=backfill)

    def _unregister_subscription(
        self, queue: "asyncio.Queue[PulseEvent]",
    ) -> None:
        """Called from Subscription.__exit__ and aclose paths."""
        with self._lock:
            self._subscribers.discard(queue)

    # Backward-compat: keep the old async-generator interface around
    # for any callers that prefer it (test files written against the
    # earlier shape, and the simpler call site for tests that don't
    # care about the registration race).
    async def subscribe(self) -> AsyncIterator[PulseEvent]:
        """Async-generator wrapper around `open_subscription`.

        Use `open_subscription()` directly if you need the subscriber
        registered before your first `await` — relevant when an HTTP
        handler yields prelude bytes before iterating, as the SSE
        route does. The wrapper here is fine for purely-iterating
        consumers that don't yield anything before their first
        `await sub.events().__anext__()`.
        """
        sub = self.open_subscription()
        try:
            for event in sub.backfill:
                yield event
            async for event in sub._live_events():
                yield event
        finally:
            sub.close()

    # ---- Internals ----

    def _record_and_fan_out(self, event: PulseEvent) -> None:
        """Record to the ring buffer, then attempt to fan out to live
        subscribers. Both operations are no-throw."""
        self._record_event(event)
        self._fan_out(event)
        self._published_events_total += 1

    def _record_event(self, event: PulseEvent) -> None:
        with self._lock:
            self._ring_buffer.append(event)

    def _fan_out(self, event: PulseEvent) -> None:
        """Push the event to every live subscriber queue.

        If no loop is bound yet (early startup), this is a no-op —
        subscribers will pick up the event from the ring buffer when
        they eventually connect.
        """
        loop = self._loop
        if loop is None:
            return

        with self._lock:
            queues = list(self._subscribers)

        for queue in queues:
            # We're calling from a worker thread. Schedule the put on
            # the event loop's thread to avoid races on the queue's
            # internal state.
            try:
                loop.call_soon_threadsafe(self._try_put, queue, event)
            except RuntimeError:
                # `call_soon_threadsafe` raises if the loop is closed.
                # That's a shutdown-window race. Drop the event quietly.
                self._dropped_events_total += 1

    def _try_put(self, queue: asyncio.Queue, event: PulseEvent) -> None:
        """Non-blocking enqueue; drops the event if the queue is full.

        Runs on the event loop thread (scheduled via
        call_soon_threadsafe). Backpressure policy: drop new events
        rather than block the loop. Operators see drops in the
        `dropped_events_total` counter."""
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_events_total += 1
            logger.warning(
                "PulseState: dropped event for slow subscriber "
                "(queue full, %d events behind)",
                queue.qsize(),
            )


class Subscription:
    """Handle to one live subscription, returned by `open_subscription`.

    Exposes the backfill snapshot synchronously and a live-events
    async generator. Designed so the SSE route can emit prelude
    bytes (`": connected"`, backfill frames) BEFORE entering its
    async-iter loop, without losing events published during that
    window.

    Use as a context manager:

        with pulse_state.open_subscription() as sub:
            for event in sub.backfill:
                yield serialize(event)         # synchronous emit
            async for event in sub.events():
                yield serialize(event)         # live emit

    The `close()` method (called from `__exit__`) unregisters the
    underlying queue from PulseState.
    """

    def __init__(
        self,
        *,
        state: PulseState,
        queue: "asyncio.Queue[PulseEvent]",
        backfill: list[PulseEvent],
    ):
        self._state = state
        self._queue = queue
        self._closed = False
        self.backfill: list[PulseEvent] = backfill
        """Snapshot of the ring buffer at the moment of subscription.
        Provided synchronously so callers can iterate without awaits
        before entering the live-events loop."""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._state._unregister_subscription(self._queue)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    async def events(self) -> AsyncIterator[PulseEvent]:
        """Async generator over live events arriving after subscription.

        Does NOT emit the backfill — callers iterate `sub.backfill`
        themselves before entering this loop. Keeping the two phases
        separate is what makes the registration-before-prelude
        ordering possible.

        Iteration terminates when `close()` is called (the queue
        receives no further events; if the caller is awaiting
        `queue.get()` at that point they must be cancelled by the
        surrounding task to escape the loop)."""
        async for event in self._live_events():
            yield event

    async def _live_events(self) -> AsyncIterator[PulseEvent]:
        """Internal: pull from the queue until close()."""
        while not self._closed:
            event = await self._queue.get()
            yield event
