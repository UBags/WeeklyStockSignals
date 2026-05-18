"""SubscriberDistributor — the 15-minute notification loop.

For each due subscriber, queries pending events (subject to ticker/signal
filters and the per-subscriber 3-notifications cap), composes a batch,
dispatches via the subscriber's transport, and records each notification
in `notification_log`.

Honors `MarketHoursService.any_open()` — outside of any market's open
hours, the distributor sleeps. (Configurable per deployment; some users
may want notifications even outside hours.)

Single thread, single-writer to notification_log. Reads are concurrent
with the ComputeScheduler's writes to signal_events; that's fine because
both operations are on append-only tables.
"""

from __future__ import annotations

import logging
import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Engine

from ..domain import NotificationBatch, SignalEvent, TickerRef
from ..observability import correlation_id, get_or_mint_correlation_id
from ..observability import metrics as M
from ..observability.tracing import (
    add_correlation_id_to_span,
    get_tracer,
)
from ..persistence import (
    SignalsRepository,
    SubscriberRecord,
    SubscribersRepository,
    transaction,
)
from .market_hours import MarketHoursService
from ..notifications.base import NotificationTransport, NullTransport

if TYPE_CHECKING:
    from ..notifications.redis_queue import RedisQueue

logger = logging.getLogger(__name__)


@dataclass
class DistributionSummary:
    started_at: datetime
    finished_at: datetime | None = None
    subscribers_due: int = 0
    subscribers_notified: int = 0
    events_dispatched: int = 0
    events_enqueued: int = 0
    """Phase 7: events handed to the Redis queue for the notifier worker.
    Used in queue-mode (when `queue` was passed to the constructor).
    `events_dispatched` is the in-process / legacy-mode counter."""
    events_skipped_dedup: int = 0
    """Phase 7: jobs that the queue rejected because the (sub, event) is
    already in flight — i.e., the previous send hasn't completed/ack'd
    yet. Not an error condition; we just wait for the next cycle."""
    delivery_failures: int = 0
    # `skipped_market_closed` and `events_suppressed_stale_date` were
    # both removed in Phase 8 B3 — the distributor no longer gates on
    # market hours or bar-bucket-open state.


class SubscriberDistributor:
    """The notification thread."""

    def __init__(
        self,
        *,
        db_engine: Engine,
        market_hours: MarketHoursService,
        transports: dict[str, NotificationTransport] | None = None,
        cadence_minutes: int = 15,
        max_event_age_hours: int = 24,
        max_notifications_each: int = 3,
        queue: "RedisQueue | None" = None,
    ):
        """Args:
            transports: Transport map for in-process / legacy mode. Used
                only when `queue` is None.
            queue: When set, the distributor operates in queue mode:
                it enqueues NotificationJob payloads to Redis instead of
                calling transports directly. The actual sends are done
                by a separate notifier worker process. The
                `notification_log` writes also move to the worker.
                When None, the distributor falls back to legacy in-process
                mode (synchronous transport calls + DB writes from this
                thread).
            market_hours: Available for any future consumer but NOT
                consulted for dispatch decisions (B3). The distributor
                always tries to deliver; `max_event_age_hours` is the
                single freshness gate.
        """
        if cadence_minutes < 1:
            raise ValueError("cadence_minutes must be >= 1")
        if queue is None and transports is None:
            raise ValueError(
                "Either `queue` (Phase 7 mode) or `transports` "
                "(legacy mode) must be provided"
            )
        self._db_engine = db_engine
        self._market_hours = market_hours
        self._transports = transports or {}
        self._queue = queue
        self._cadence = timedelta(minutes=cadence_minutes)
        self._max_event_age = timedelta(hours=max_event_age_hours)
        self._max_each = max_notifications_each
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run_completed_at: datetime | None = None
        self._was_started: bool = False

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def transports(self) -> dict[str, NotificationTransport]:
        """Read-only view of the legacy-mode transport map. Empty dict
        in queue mode (in queue mode the transports live in the
        notifier subprocess, not here). Used by the
        /v1/admin/circuit-breakers endpoint to surface
        CircuitBreakingTransport state."""
        return dict(self._transports)

    def is_unhealthy(self, now: datetime, stale_after_multiplier: float = 2.0) -> str | None:
        """Return a description of why this distributor is unhealthy, or
        None if fine. Same semantics as ComputeScheduler.is_unhealthy."""
        if not self._was_started:
            return None
        if not self.is_alive():
            return "SubscriberDistributor thread died"
        stale_threshold = self._cadence * stale_after_multiplier
        if self.last_run_completed_at is None:
            return None
        gap = now - self.last_run_completed_at
        if gap > stale_threshold:
            return (
                f"SubscriberDistributor hasn't completed in {gap.total_seconds():.0f}s "
                f"(threshold: {stale_threshold.total_seconds():.0f}s)"
            )
        return None

    # ---------- Unit of work ---------- #

    def run_once(self) -> DistributionSummary:
        now = datetime.now(timezone.utc)
        summary = DistributionSummary(started_at=now)

        tracer = get_tracer("trip_ema.subscriber_distributor")
        with tracer.start_as_current_span(
            "subscriber_distributor.run_once",
        ) as span:
            add_correlation_id_to_span()

            # B3: the distributor no longer consults market hours. The system
            # informs subscribers ASAP regardless of whether the market is open.
            # It's the trader's responsibility to track and act on signals;
            # not the system's job to throttle delivery around exchange hours.
            # `MarketHoursService` remains on `self._market_hours` for any
            # future consumer that needs it, but it does not gate dispatch.

            # 1. Find subscribers whose cadence has elapsed.
            with self._db_engine.connect() as conn:
                due = SubscribersRepository(conn).find_due_for_notification(now)
            summary.subscribers_due = len(due)
            span.set_attribute("subscribers.due", len(due))

            for subscriber in due:
                try:
                    dispatched = self._notify_one(subscriber, now, summary)
                    # The inner methods (_dispatch_in_process / _enqueue_jobs)
                    # already update events_dispatched / events_enqueued on
                    # the summary. We only count "this subscriber got
                    # something" here.
                    if dispatched > 0:
                        summary.subscribers_notified += 1
                except Exception as e:
                    logger.exception(
                        "Failed to notify subscriber",
                        extra={"subscriber_id": subscriber.subscriber_id},
                    )
                    summary.delivery_failures += 1
                    span.record_exception(e)

            summary.finished_at = datetime.now(timezone.utc)
            self.last_run_completed_at = summary.finished_at
            span.set_attribute("subscribers.notified", summary.subscribers_notified)
            span.set_attribute("events.dispatched", summary.events_dispatched)
            span.set_attribute("events.enqueued", summary.events_enqueued)
            logger.info(
                "Distribution run completed",
                extra={
                    "subscribers_due": summary.subscribers_due,
                    "subscribers_notified": summary.subscribers_notified,
                    "events_dispatched": summary.events_dispatched,
                    "events_enqueued": summary.events_enqueued,
                    "delivery_failures": summary.delivery_failures,
                },
            )
            return summary

    def _notify_one(
        self,
        subscriber: SubscriberRecord,
        now: datetime,
        summary: DistributionSummary,
    ) -> int:
        """Look up pending events for this subscriber, then either:
          - Queue mode (`self._queue` set): enqueue one job per event.
            The notifier worker dispatches and writes notification_log.
            Returns the count of events enqueued.
          - Legacy mode (`self._queue` None): dispatch synchronously
            and write notification_log directly. Returns the count of
            events dispatched.
        """

        with self._db_engine.connect() as conn:
            pending = SignalsRepository(conn).find_pending_for_subscriber(
                subscriber.subscriber_id,
                max_age=self._max_event_age,
                max_notifications_each=self._max_each,
                now=now,
            )

        if not pending:
            return 0

        # B3 (Phase 8): no longer filter by bucket-open / bar-date.
        # The `max_event_age` filter on `find_pending_for_subscriber`
        # (default 24h) is now the single freshness gate: events older
        # than that age out and stop pinging; everything else delivers.
        # Friday 15:00 signal → pings Friday 15:15/15:30/15:45 (after
        # market close — that's the point), then either the cap (3
        # sends) or the 24h age cap stops it.

        if self._queue is not None:
            return self._enqueue_jobs(subscriber, pending, now, summary)
        return self._dispatch_in_process(subscriber, pending, now, summary)

    def _enqueue_jobs(
        self,
        subscriber: SubscriberRecord,
        pending: list[tuple[int, SignalEvent, TickerRef]],
        now: datetime,
        summary: DistributionSummary,
    ) -> int:
        """Phase 7 path: one Redis job per (subscriber, event) pair.

        The notifier worker is responsible for the actual send + writing
        the notification_log row + updating last_notified_at. This thread
        just enqueues and returns.

        Dedup: if the same (sub, event) is already in flight (the previous
        send hasn't completed), the enqueue is rejected. The next cycle
        will try again — by which point the worker should have ack'd and
        the dedup slot will be free.
        """
        enqueued = 0
        for event_id, event, ticker_ref in pending:
            job_id = f"{subscriber.subscriber_id}:{event_id}"
            # Mint or reuse a correlation ID for this notification.
            # The worker will push this onto its context on dispatch,
            # so logs across distributor → Redis → worker → transport
            # all share the same ID.
            cid = get_or_mint_correlation_id()
            payload = {
                "subscriber_id": subscriber.subscriber_id,
                "transport": subscriber.transport,
                "address": subscriber.address,
                "event_id": event_id,
                "correlation_id": cid,
                "event": {
                    "ticker": {
                        "exchange": ticker_ref.exchange,
                        "symbol": ticker_ref.symbol,
                        "instrument": ticker_ref.instrument,
                    },
                    "timeframe": event.timeframe.value,
                    "bar_timestamp": event.bar_timestamp.isoformat(),
                    "signal": event.signal.name,
                    "price_at_signal": event.price_at_signal,
                    "computed_at": event.computed_at.isoformat(),
                },
                "enqueued_at": now.isoformat(),
            }
            if self._queue.enqueue(job_id, payload):
                enqueued += 1
                M.distributor_jobs_enqueued_total.labels(
                    transport=subscriber.transport,
                ).inc()
            else:
                summary.events_skipped_dedup += 1
        summary.events_enqueued += enqueued
        return enqueued

    def _dispatch_in_process(
        self,
        subscriber: SubscriberRecord,
        pending: list[tuple[int, SignalEvent, TickerRef]],
        now: datetime,
        summary: DistributionSummary,
    ) -> int:
        """Legacy mode: dispatch all events as one batched message,
        write notification_log inline."""
        event_ids = [eid for eid, _, _ in pending]
        events: list[SignalEvent] = [e for _, e, _ in pending]

        transport = self._transports.get(subscriber.transport, NullTransport())
        batch = NotificationBatch(
            subscriber_id=subscriber.subscriber_id,
            events=events,
            composed_at=now,
        )
        ok = transport.send(subscriber.address, batch)

        if not ok:
            logger.warning(
                "Transport %s failed for subscriber %s; will retry next cycle",
                subscriber.transport, subscriber.subscriber_id,
            )
            return 0

        with transaction(self._db_engine) as conn:
            subs_repo = SubscribersRepository(conn)
            subs_repo.record_batch(
                subscriber_id=subscriber.subscriber_id,
                event_ids=event_ids,
                sent_at=now,
            )
            subs_repo.update_last_notified(subscriber.subscriber_id, now)

        summary.events_dispatched += len(events)
        return len(events)

    # ---------- Daemon thread mode ---------- #

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="SubscriberDistributor", daemon=True,
        )
        self._thread.start()
        self._was_started = True
        logger.info("SubscriberDistributor started (cadence=%s)", self._cadence)

    def stop(self, timeout: float | None = 30.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("SubscriberDistributor stopped")

    def _loop(self) -> None:
        from .compute_scheduler import _seconds_until_next_tick
        while not self._stop_event.is_set():
            sleep_seconds = _seconds_until_next_tick(
                datetime.now(timezone.utc), self._cadence,
            )
            slept = 0.0
            while slept < sleep_seconds and not self._stop_event.is_set():
                chunk = min(1.0, sleep_seconds - slept)
                time_module.sleep(chunk)
                slept += chunk
            if self._stop_event.is_set():
                break
            try:
                self.run_once()
            except Exception:
                logger.exception("Unhandled error in distribution loop; continuing")
