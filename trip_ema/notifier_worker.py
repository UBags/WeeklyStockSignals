"""Notifier worker — consumes the Redis queue and dispatches sends.

Runs as a separate process from the main trip_ema app. The distributor
enqueues NotificationJob payloads to Redis; this worker pops them,
dispatches via the configured transport, writes notification_log + updates
last_notified_at on success, then acks the dedup slot.

On send failure: do NOT ack. The dedup slot stays held until either
  (a) the dedup-TTL sweep clears it (worker crashed mid-send), or
  (b) the worker process retries the same job on the next pop.

Currently we only support (a) — the worker drops failed jobs and the
distributor's next cycle is free to re-enqueue (subject to whether the
dedup slot was cleared). This matches the per-cycle re-derivation
contract from the design phase: every distributor cycle independently
finds the same pending events, so failed delivery becomes a "retry next
cycle" by emergent behavior, not explicit logic.

Process lifecycle:
  - main() builds the worker from a Config, then runs run_forever()
  - run_forever() loops: pop one job, dispatch via ThreadPoolExecutor
    (so I/O-bound transports can fan out), ack on success
  - SIGTERM/SIGINT: stop popping, wait for in-flight executor tasks to
    finish (up to `shutdown_grace_seconds`), then exit
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import redis
import sqlalchemy as sa

from .config import Config, load_config
from .config.schema import RedisConfig
from .domain import (
    ChangeSignal,
    NotificationBatch,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from .notifications.base import NotificationTransport, NullTransport
from .notifications.email_transport import EmailTransport
from .notifications.redis_queue import RedisQueue
from .notifications.webhook_transport import WebhookTransport
from .observability import correlation_id
from .observability import metrics as M
from .observability.tracing import (
    add_correlation_id_to_span,
    get_tracer,
)
from .persistence import (
    SignalsRepository,
    SubscribersRepository,
    create_engine_from_config,
    transaction,
)

logger = logging.getLogger(__name__)


class NotifierWorker:
    """The worker process logic, factored out so it's unit-testable
    without spawning a process."""

    def __init__(
        self,
        *,
        queue: RedisQueue,
        db_engine: sa.Engine,
        transports: dict[str, NotificationTransport],
        worker_count: int = 4,
        pop_timeout_seconds: int = 5,
        max_attempts: int = 5,
    ):
        if worker_count < 1:
            raise ValueError("worker_count must be >= 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._queue = queue
        self._db_engine = db_engine
        self._transports = transports
        self._worker_count = worker_count
        self._pop_timeout = pop_timeout_seconds
        self._max_attempts = max_attempts

        self._stop_event = threading.Event()
        self._executor: ThreadPoolExecutor | None = None

        # Counters for tests and ops visibility
        self.jobs_processed = 0
        self.jobs_succeeded = 0
        self.jobs_failed = 0
        self.jobs_dropped_stale = 0
        self.jobs_moved_to_dlq = 0
        """Phase 12: count of jobs the worker gave up on and moved
        to the DLQ after exceeding max_attempts. Operators can
        replay them via the admin endpoint once the underlying
        issue is fixed."""

    def run_forever(self) -> None:
        """Main loop. Pop one job at a time; dispatch via the executor;
        do not block waiting for the executor between pops — but cap
        in-flight at `worker_count` by submitting only when there's a
        free slot. (A semaphore is the simplest pattern.)"""
        self._executor = ThreadPoolExecutor(
            max_workers=self._worker_count,
            thread_name_prefix="notifier-send",
        )
        in_flight = threading.Semaphore(self._worker_count)

        logger.info("NotifierWorker started (workers=%d)", self._worker_count)

        try:
            while not self._stop_event.is_set():
                # Block here until a slot frees up
                if not in_flight.acquire(timeout=1.0):
                    continue
                if self._stop_event.is_set():
                    in_flight.release()
                    break

                job = self._queue.pop_blocking(self._pop_timeout)
                if job is None:
                    in_flight.release()
                    continue

                # Hand off to the executor and release the slot on completion.
                future = self._executor.submit(self._handle_job, job)
                future.add_done_callback(lambda _f: in_flight.release())
        finally:
            logger.info("NotifierWorker stopping; waiting for in-flight sends")
            self._executor.shutdown(wait=True)
            self._executor = None
            logger.info(
                "NotifierWorker stopped (processed=%d, ok=%d, failed=%d)",
                self.jobs_processed, self.jobs_succeeded, self.jobs_failed,
            )

    def stop(self) -> None:
        """Request shutdown — the loop will exit after the current iteration."""
        self._stop_event.set()

    def run_once_for_test(self, max_jobs: int = 1) -> int:
        """Test helper: pop up to `max_jobs` jobs from the queue and
        process them inline (no executor, no semaphore). Returns the
        count processed. Use this in unit tests to drive the worker
        deterministically without thread-timing concerns."""
        processed = 0
        for _ in range(max_jobs):
            job = self._queue.pop_blocking(timeout_seconds=1)
            if job is None:
                break
            self._handle_job(job)
            processed += 1
        return processed

    def _handle_job(self, job: dict[str, Any]) -> None:
        """Process one job: dispatch + log + ack on success.

        NEW 3.4 (Phase 9): also handles the 'superseded' outcome — when
        a newer signal_event for the same (ticker, timeframe) exists at
        dispatch time, we drop the stale job and ack the dedup slot.
        That's a *successful* drop (no error, no retry), distinct from
        a transport failure (which nacks for retry on the next cycle).

        Phase 10 observability: pushes the correlation_id from the
        payload onto the current context, so all log records written
        during dispatch carry it. Increments per-transport metrics by
        outcome. Measures dispatch duration via a histogram.

        Errors here are logged and counted but never propagate — we
        don't want one bad payload to take down the worker.
        """
        self.jobs_processed += 1
        job_id = job.get("_job_id", "?")
        # Pull the correlation ID off the payload (set by the
        # distributor). Older payloads without one fall through with
        # None — logs simply omit the field.
        cid = job.get("correlation_id")
        transport_name = job.get("transport", "unknown")
        with correlation_id(cid):
            tracer = get_tracer("trip_ema.notifier_worker")
            with tracer.start_as_current_span(
                "notifier_worker.dispatch_job",
            ) as span:
                add_correlation_id_to_span()
                span.set_attribute("transport", transport_name)
                span.set_attribute("job_id", str(job_id))
                if "event_id" in job:
                    span.set_attribute("event_id", job["event_id"])
                try:
                    with M.notifier_dispatch_duration_seconds.labels(
                        transport=transport_name,
                    ).time():
                        outcome = self._dispatch_and_log(job)
                    span.set_attribute("outcome", outcome)
                    if outcome == "sent":
                        self._queue.ack(job_id)
                        self.jobs_succeeded += 1
                        M.notifier_jobs_processed_total.labels(
                            outcome="sent", transport=transport_name,
                        ).inc()
                    elif outcome == "superseded":
                        # Intentional drop: a newer event exists. Free the dedup
                        # slot so any *new* job for the same (sub, event) can be
                        # enqueued — though there won't be one for this event_id;
                        # the distributor's next cycle will enqueue for the newer
                        # event_id instead.
                        self._queue.ack(job_id)
                        self.jobs_dropped_stale += 1
                        M.notifier_jobs_processed_total.labels(
                            outcome="superseded", transport=transport_name,
                        ).inc()
                    elif outcome == "failed":
                        self.jobs_failed += 1
                        # Phase 12: bump persistent attempt counter
                        # (lives in Redis, not on the payload — so it
                        # survives the distributor re-creating the
                        # payload on the next cycle). If we've
                        # exhausted retries, move to DLQ instead of
                        # nacking back into the queue. Without this,
                        # a job whose transport is circuit-broken
                        # cycles forever — distributor re-enqueues
                        # every cycle, worker fails every cycle.
                        attempts = self._queue.increment_attempts(job_id)
                        if attempts >= self._max_attempts:
                            logger.warning(
                                "Job exceeded max_attempts; moving to DLQ",
                                extra={
                                    "job_id": job_id,
                                    "attempts": attempts,
                                    "max_attempts": self._max_attempts,
                                    "transport": transport_name,
                                },
                            )
                            job["_attempts"] = attempts
                            self._queue.move_to_dlq(
                                job,
                                reason=f"max_attempts ({self._max_attempts}) exceeded",
                            )
                            self.jobs_moved_to_dlq += 1
                            M.notifier_jobs_processed_total.labels(
                                outcome="dlq", transport=transport_name,
                            ).inc()
                        else:
                            # Has retries left — nack as before so
                            # the distributor's next cycle can
                            # re-enqueue. The attempts counter we
                            # just bumped persists; the next failure
                            # finds attempts already at N.
                            self._queue.nack(job_id)
                            M.notifier_jobs_processed_total.labels(
                                outcome="failed", transport=transport_name,
                            ).inc()
                    else:
                        # Defensive — unknown outcome string. Treat as failure.
                        logger.error(
                            "Unknown dispatch outcome; nacking",
                            extra={"outcome": outcome, "job_id": job_id},
                        )
                        self.jobs_failed += 1
                        self._queue.nack(job_id)
                        M.notifier_jobs_processed_total.labels(
                            outcome="failed", transport=transport_name,
                        ).inc()
                except Exception as e:
                    self.jobs_failed += 1
                    span.set_attribute("error", True)
                    span.record_exception(e)
                    logger.exception(
                        "Unhandled error processing job",
                        extra={"job_id": job_id},
                    )
                    # Also nack on unhandled exception so we don't lock the slot
                    # indefinitely because of a malformed payload.
                    try:
                        self._queue.nack(job_id)
                    except Exception:
                        logger.exception("nack also failed", extra={"job_id": job_id})
                    M.notifier_jobs_processed_total.labels(
                        outcome="failed", transport=transport_name,
                    ).inc()

    def _dispatch_and_log(self, job: dict[str, Any]) -> str:
        """Reconstruct the SignalEvent from the JSON payload, check
        whether it's been superseded, dispatch via the transport, write
        notification_log on success.

        Returns one of:
          "sent"       — transport.send returned True; notification_log
                         row written; last_notified_at updated. Caller
                         should ack the dedup slot.
          "superseded" — NEW 3.4 (Phase 9): a strictly-newer event for
                         the same (ticker, timeframe) exists at dispatch
                         time. The job is dropped without sending or
                         writing notification_log. The newer event will
                         be picked up by the distributor's next cycle.
                         Caller should ack the dedup slot.
          "failed"     — transport.send returned False. Caller should
                         nack so the distributor re-enqueues next cycle.
        """
        subscriber_id = int(job["subscriber_id"])
        transport_name = job["transport"]
        address = job["address"]
        event_id = int(job["event_id"])
        ev = job["event"]

        ticker_ref = TickerRef(
            exchange=ev["ticker"]["exchange"],
            symbol=ev["ticker"]["symbol"],
            instrument=ev["ticker"]["instrument"],
        )
        signal_event = SignalEvent(
            ticker=ticker_ref,
            timeframe=Timeframe(ev["timeframe"]),
            bar_timestamp=datetime.fromisoformat(ev["bar_timestamp"]),
            signal=ChangeSignal[ev["signal"]],
            price_at_signal=float(ev["price_at_signal"]),
            computed_at=datetime.fromisoformat(ev["computed_at"]),
        )

        # NEW 3.4 (Phase 9): last-moment freshness check. A BUY job that
        # was enqueued before an EXIT was generated may be sitting in
        # the Redis queue; sending it now would deliver a stale signal
        # AFTER the newer one had already been (or would be) sent. Drop
        # the stale job by ack'ing without sending.
        with self._db_engine.connect() as conn:
            superseded = SignalsRepository(conn).is_event_superseded(event_id)
        if superseded:
            logger.info(
                "Dropping superseded job: subscriber=%d event=%d %s@%s "
                "(a newer event exists for this ticker/timeframe)",
                subscriber_id, event_id,
                f"{ticker_ref.exchange}:{ticker_ref.symbol}",
                signal_event.timeframe.value,
            )
            return "superseded"

        transport = self._transports.get(transport_name, NullTransport())
        batch = NotificationBatch(
            subscriber_id=subscriber_id,
            events=[signal_event],
            composed_at=datetime.now(UTC),
        )
        ok = transport.send(address, batch)
        if not ok:
            logger.warning(
                "Send failed: transport=%s subscriber=%d event=%d",
                transport_name, subscriber_id, event_id,
            )
            return "failed"

        # Write the notification log + bump last_notified_at, atomically.
        now = datetime.now(UTC)
        with transaction(self._db_engine) as conn:
            subs_repo = SubscribersRepository(conn)
            subs_repo.record_notification(
                subscriber_id=subscriber_id,
                event_id=event_id,
                sent_at=now,
            )
            subs_repo.update_last_notified(subscriber_id, now)
        return "sent"


# ---------- Process entry point ---------- #


def _build_transports_from_config(config: Config) -> dict[str, NotificationTransport]:
    """Build the transport map from `config.transports`. The worker
    process needs its own copy of the build logic since it doesn't
    share Python state with the main app process — but the SOURCE
    OF TRUTH (config.yaml + secrets file) is shared, so both
    processes deterministically produce the same transports.

    Note: there is NO env-var fallback path here — see the same
    note in app.py::_build_email_transport_or_null. A misconfigured
    transport.brevo would already have failed startup at load_config().
    """
    transports: dict[str, NotificationTransport] = {
        "webhook": WebhookTransport(),
    }
    brevo_cfg = config.transports.brevo
    if brevo_cfg is None:
        logger.info(
            "transports.brevo not configured — using NullTransport for email"
        )
        transports["email"] = NullTransport()
    else:
        from .config.brevo_secrets import load_brevo_api_key
        api_key = load_brevo_api_key(brevo_cfg.secrets_file)
        logger.info(
            "Email transport: Brevo HTTP API (sender=%s, endpoint=%s)",
            brevo_cfg.sender_email, brevo_cfg.api_url,
        )
        transports["email"] = EmailTransport(
            api_key=api_key,
            from_address=brevo_cfg.sender_email,
            from_name=brevo_cfg.sender_name,
            subject=brevo_cfg.subject,
            api_url=brevo_cfg.api_url,
            timeout_seconds=brevo_cfg.timeout_seconds,
        )
    return transports


def _build_redis_client(config: RedisConfig) -> redis.Redis:
    import os
    password = None
    if config.password_env:
        password = os.environ.get(config.password_env)
    return redis.Redis(
        host=config.host,
        port=config.port,
        db=config.db,
        password=password,
        decode_responses=False,   # RedisQueue handles encoding
    )


def _wrap_transports_with_breakers(
    transports: dict[str, NotificationTransport],
    config: Config,
) -> dict[str, NotificationTransport]:
    """Mirror of the helper in app.py — kept duplicated to avoid a
    cross-module dep between notifier_worker (intentionally
    self-contained for separate-process deployment) and app.py.

    If circuit-breakers are enabled in config, wrap each transport with
    a CircuitBreakingTransport instance. Otherwise return as-is.
    """
    cb_cfg = config.transports.circuit_breaker
    if not cb_cfg.enabled:
        return transports

    from .notifications.circuit_breaker import (
        CircuitBreakerConfig as BreakerConfig,
        CircuitBreakingTransport,
    )
    bcfg = BreakerConfig(
        failure_threshold=cb_cfg.failure_threshold,
        reset_timeout_seconds=cb_cfg.reset_timeout_seconds,
        half_open_max_successes=cb_cfg.half_open_max_successes,
    )
    return {
        name: CircuitBreakingTransport(t, name=name, config=bcfg)
        for name, t in transports.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trip_ema.notifier_worker")
    parser.add_argument(
        "--config", "-c", type=Path, required=True,
        help="Path to YAML config file (same file as the main app)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config: Config = load_config(args.config)
    if config.redis is None or config.notifier is None:
        logger.error(
            "notifier_worker requires both `redis` and `notifier` sections "
            "in the config file"
        )
        return 2

    redis_client = _build_redis_client(config.redis)
    queue = RedisQueue(
        redis_client=redis_client,
        queue_name=config.redis.queue_name,
        dedup_hash_name=config.redis.dedup_hash_name,
    )
    db_engine = create_engine_from_config(config)
    transports = _build_transports_from_config(config)
    transports = _wrap_transports_with_breakers(transports, config)

    worker = NotifierWorker(
        queue=queue,
        db_engine=db_engine,
        transports=transports,
        worker_count=config.notifier.worker_count,
        pop_timeout_seconds=config.notifier.pop_timeout_seconds,
        max_attempts=config.notifier.max_attempts,
    )

    # Graceful shutdown on signals
    def _shutdown(signum, frame):
        logger.info("Signal %s received; shutting down notifier worker", signum)
        worker.stop()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        worker.run_forever()
    finally:
        db_engine.dispose()
        try:
            redis_client.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
