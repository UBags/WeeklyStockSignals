"""MarketOrchestrator — per-market hourly pipeline driver.

One MarketOrchestrator instance per exchange. The orchestrator owns
a wall-clock-anchored loop (via tick_planner.next_tick_after) that
fires at the per-market schedule (post-open + hourly + pre-close,
on trading days only). Each tick runs the pipeline:

    fetch (yfinance/EOD)  →  compute (TickerEngine)  →  distribute (notify)

…then publishes a summary to the PulsePublisher for GUI consumption.

Design choices, with rationale:

* Single-thread sequencing of three sequential steps, not three threads
  with message passing. Each step's output is the next's input; ordering
  is trivially correct; debugging is straightforward. Three-actor designs
  pay coordination tax without buying anything here.

* `run_once()` is the public unit of work. Reentrancy-protected. Callable
  from the loop thread, from a retry-scheduled task, or from an admin
  HTTP endpoint. Concurrent invocations get `skipped_due_to_concurrent_run`.

* Each pipeline step runs through a small `_run_*_step` wrapper that
  catches exceptions, records them on the summary, and *continues to the
  next step*. A failed fetch shouldn't abort compute (existing DB bars
  are still useful); a failed compute shouldn't abort distribute (pending
  events from prior passes may still be deliverable).

* Fetch has a hard wall-clock timeout (`fetch_timeout_seconds`) enforced
  via a single-worker ThreadPoolExecutor + `future.result(timeout=...)`.
  `signal.alarm()` won't work because the loop runs on a non-main thread.
  The underlying yfinance call can't actually be interrupted on timeout,
  but the orchestrator thread is freed and the pipeline proceeds.

* On fetch failure, schedule exactly one retry after `fetch_retry_delay_minutes`
  on a separate retry executor. The retry, if successful, also runs compute
  + distribute (since the original pipeline's compute ran against stale
  data). If the retry also fails, log and stop — the next scheduled tick
  is the natural next attempt. No exponential stacking, no retry-of-retry.

* Threading model:
    start() spawns a daemon thread that loops:
        sleep until next_tick_after(now, market, calendar)
        run_once()
    stop() flips a flag + cancels pending retries + shuts down both executors.

* What is NOT here:
    - Multi-market coordination: that's OrchestratorPool (Step 4).
    - The PulseState pub/sub itself: only the Protocol is here; the
      concrete class lives in Step 4. This lets tests inject a Mock.
    - Admin HTTP endpoints: Step 5.
    - app.py wiring: Step 6.
"""

from __future__ import annotations

import logging
import threading
import time as time_module
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from typing import Any, Protocol
from collections.abc import Callable

from ..observability import correlation_id, new_correlation_id
from .market_calendar import TradingCalendar
from .tick_planner import (
    MarketTickConfig,
    compute_tick_times_for_day,
    next_tick_after,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorStepResult:
    """One pipeline step's outcome. The summary holds three of these."""
    succeeded: bool = False
    skipped_reason: str | None = None
    """If set, the step was not even attempted (e.g. non-trading day for
    fetch). Distinguishable from a failure: skipped is NOT an error."""
    error_message: str | None = None
    """Set on exception. Mutually exclusive with skipped_reason."""
    duration_seconds: float = 0.0
    detail: Any = None
    """Caller-defined payload: ComputeRunSummary for compute,
    DistributionSummary for distribute, fetch run_id for fetch, etc.
    Kept loosely-typed since each step has its own summary shape and
    we don't want to leak all those types into this module."""


@dataclass
class OrchestratorRunSummary:
    """Result of one orchestrator.run_once() pass.

    Mirrors the shape of ComputeRunSummary but at the pipeline level.
    Carries enough information for the pulse payload, admin status,
    and tests."""
    exchange: str
    started_at: datetime
    finished_at: datetime | None = None
    correlation_id: str = ""

    fetch: OrchestratorStepResult = field(default_factory=OrchestratorStepResult)
    compute: OrchestratorStepResult = field(default_factory=OrchestratorStepResult)
    distribute: OrchestratorStepResult = field(
        default_factory=OrchestratorStepResult
    )

    skipped_due_to_concurrent_run: bool = False
    """True if run_once() returned early because another run_once was
    already in flight. None of fetch/compute/distribute will have run."""

    retry_scheduled: bool = False
    """True if a retry was scheduled for the fetch step. The retry runs
    on a separate executor and produces its own summary."""

    is_retry: bool = False
    """True for summaries produced by retry runs (not by the regular loop
    or admin run-now). Lets the GUI/logs distinguish retry events."""

    @property
    def all_steps_ok(self) -> bool:
        return (
            self.fetch.succeeded
            and self.compute.succeeded
            and self.distribute.succeeded
        )

    @property
    def total_duration_seconds(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()


class PulsePublisher(Protocol):
    """Protocol the orchestrator uses to broadcast pulse events.

    The concrete implementation (Step 4) is an in-memory ring buffer +
    asyncio broadcast. Tests pass a Mock. Decoupling via a Protocol
    means Step 3 can be merged independently of Step 4.
    """
    def publish(self, summary: OrchestratorRunSummary) -> None: ...


# Callable signatures the orchestrator depends on. Using Callable rather
# than the concrete classes (ComputeScheduler etc.) keeps test setup tiny
# and avoids dragging the full scheduler module into pure logic tests.
FetchFn = Callable[[str], Any]
"""Takes exchange name, returns whatever the runner returns (run_id).
Raises on failure. The orchestrator only cares about success vs exception
and the return-value-as-opaque-detail."""

ComputeRunOnceFn = Callable[[], Any]
"""Calls compute_scheduler.run_once(). Returns ComputeRunSummary; the
orchestrator stores it as an opaque detail on the step result."""

DistributeRunOnceFn = Callable[[], Any]
"""Calls subscriber_distributor.run_once(). Returns DistributionSummary."""


# ---------------------------------------------------------------------------
# MarketOrchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrchestratorSettings:
    """Runtime settings, derived from OrchestratorConfig at app.py
    wiring time. Kept as a small frozen dataclass so the orchestrator
    doesn't have to know about Pydantic models or the full Config tree."""
    fetch_timeout: timedelta
    fetch_retry_delay: timedelta
    catch_up_on_startup: bool
    catch_up_skip_if_recent: timedelta


class MarketOrchestrator:
    """One orchestrator per market.

    Owns the per-market tick loop, the fetch retry executor, and (via
    `last_run_summary` / pulse publishes) the per-market state surfaced
    to GUI and admin endpoints.
    """

    def __init__(
        self,
        *,
        market: MarketTickConfig,
        calendar: TradingCalendar,
        settings: OrchestratorSettings,
        fetch_fn: FetchFn,
        compute_run_once: ComputeRunOnceFn,
        distribute_run_once: DistributeRunOnceFn,
        pulse: PulsePublisher | None = None,
        # Clock injection for deterministic tests. Production passes
        # nothing; the default reads the real wall clock.
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep_fn: Callable[[float], None] = time_module.sleep,
    ):
        self._market = market
        self._calendar = calendar
        self._settings = settings
        self._fetch_fn = fetch_fn
        self._compute_run_once = compute_run_once
        self._distribute_run_once = distribute_run_once
        self._pulse = pulse
        self._now_fn = now_fn
        self._sleep_fn = sleep_fn

        # Reentrancy guard. Held during a run_once() call; another
        # run_once (whether from the loop, retry, or admin endpoint)
        # returns skipped if it can't acquire.
        self._run_lock = threading.Lock()

        # Loop thread management.
        self._loop_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._was_started = False

        # Fetch executor: bounded to 1 in-flight fetch per orchestrator.
        # max_workers=2 leaves headroom for one pending retry to coexist
        # with a normal fetch if the operator manually forces a run-now
        # during the retry window.
        self._fetch_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"orch-{market.exchange}-fetch",
        )
        # Retry executor: holds the scheduled retry timer. Separate so
        # an exhausted fetch_executor (long-running fetch) doesn't block
        # the retry timer's ability to fire.
        self._retry_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"orch-{market.exchange}-retry",
        )
        # Track the in-flight retry so stop() can cancel it cleanly.
        self._pending_retry: Future | None = None
        self._pending_retry_lock = threading.Lock()

        # Public-readable state. Updated under _run_lock; readers should
        # treat as best-effort snapshots, not strongly consistent.
        self.last_run_summary: OrchestratorRunSummary | None = None
        self.last_run_completed_at: datetime | None = None

    # ---- Public lifecycle ----

    def start(self) -> None:
        """Spawn the loop thread. Idempotent: a second call while alive
        is a no-op."""
        if self._loop_thread is not None and self._loop_thread.is_alive():
            logger.warning(
                "MarketOrchestrator[%s].start() called while already running",
                self._market.exchange,
            )
            return
        self._was_started = True
        self._stop_event.clear()
        self._loop_thread = threading.Thread(
            target=self._loop,
            name=f"orch-{self._market.exchange}",
            daemon=True,
        )
        self._loop_thread.start()
        logger.info("MarketOrchestrator[%s] started", self._market.exchange)

    def stop(self, timeout: float = 30.0) -> None:
        """Signal the loop to exit and shut down executors.

        Bounded by `timeout` total wall-clock seconds. After timeout,
        in-flight tasks are abandoned (daemon threads will die with
        the process)."""
        self._stop_event.set()

        # Cancel pending retry first so it doesn't fire after shutdown.
        with self._pending_retry_lock:
            if self._pending_retry is not None:
                self._pending_retry.cancel()
                self._pending_retry = None

        if self._loop_thread is not None:
            self._loop_thread.join(timeout=timeout)
            self._loop_thread = None

        self._fetch_executor.shutdown(wait=False, cancel_futures=True)
        self._retry_executor.shutdown(wait=False, cancel_futures=True)
        logger.info("MarketOrchestrator[%s] stopped", self._market.exchange)

    def is_alive(self) -> bool:
        return self._loop_thread is not None and self._loop_thread.is_alive()

    def is_unhealthy(self) -> bool:
        """For /readyz. The orchestrator is unhealthy if start() was
        called but the loop thread has died. A never-started orchestrator
        is reported healthy (i.e., it was disabled, not broken)."""
        if not self._was_started:
            return False
        return self._loop_thread is None or not self._loop_thread.is_alive()

    @property
    def exchange(self) -> str:
        return self._market.exchange

    # ---- Public unit of work ----

    def run_once(self, *, is_retry: bool = False) -> OrchestratorRunSummary:
        """Execute one fetch → compute → distribute pass synchronously.

        Reentrancy-protected. If called while another run_once is in
        flight, returns immediately with `skipped_due_to_concurrent_run=True`
        and none of the steps run.

        `is_retry`: True when called from the retry executor. Affects
        logging and the summary flag; otherwise the pipeline runs the
        same way.
        """
        if not self._run_lock.acquire(blocking=False):
            return OrchestratorRunSummary(
                exchange=self._market.exchange,
                started_at=self._now_fn(),
                skipped_due_to_concurrent_run=True,
                is_retry=is_retry,
            )
        try:
            return self._run_once_locked(is_retry=is_retry)
        finally:
            self._run_lock.release()

    # ---- Internals ----

    def _run_once_locked(self, *, is_retry: bool) -> OrchestratorRunSummary:
        """The body of run_once, executed while holding _run_lock."""
        cid = new_correlation_id()
        started = self._now_fn()
        summary = OrchestratorRunSummary(
            exchange=self._market.exchange,
            started_at=started,
            correlation_id=cid,
            is_retry=is_retry,
        )

        with correlation_id(cid):
            logger.info(
                "Orchestrator[%s] %s pipeline starting",
                self._market.exchange,
                "retry" if is_retry else "regular",
                extra={"correlation_id": cid},
            )

            # ---- Step 1: Fetch ----
            # Trading-day gate. yfinance EOD on a non-trading day
            # returns nothing useful and risks creating an empty run row.
            today_local = self._now_fn().astimezone(self._market.tz).date()
            if not self._calendar.is_trading_day(today_local):
                summary.fetch.skipped_reason = (
                    f"non-trading day {today_local.isoformat()}"
                )
                summary.fetch.succeeded = True  # skip is NOT failure
                logger.info(
                    "Orchestrator[%s] fetch skipped: %s",
                    self._market.exchange, summary.fetch.skipped_reason,
                )
            else:
                self._run_fetch_step(summary, allow_retry_schedule=not is_retry)

            # ---- Step 2: Compute ----
            self._run_compute_step(summary)

            # ---- Step 3: Distribute ----
            self._run_distribute_step(summary)

            summary.finished_at = self._now_fn()
            # Only update last_run_completed_at for successful or
            # partially-successful passes — concurrent-skipped passes
            # never reach here.
            self.last_run_completed_at = summary.finished_at
            self.last_run_summary = summary

            logger.info(
                "Orchestrator[%s] pipeline complete in %.2fs "
                "(fetch=%s, compute=%s, distribute=%s)",
                self._market.exchange,
                summary.total_duration_seconds,
                _step_label(summary.fetch),
                _step_label(summary.compute),
                _step_label(summary.distribute),
                extra={"correlation_id": cid},
            )

            self._publish_pulse_safely(summary)
            return summary

    def _run_fetch_step(
        self, summary: OrchestratorRunSummary, *, allow_retry_schedule: bool
    ) -> None:
        """Run fetch with timeout. On failure, optionally schedule a retry.

        `allow_retry_schedule=False` is passed when *this* run_once is
        itself a retry — we don't want to chain retries-of-retries.
        """
        step = summary.fetch
        t0 = time_module.monotonic()
        try:
            future = self._fetch_executor.submit(
                self._fetch_fn, self._market.exchange
            )
            timeout_seconds = self._settings.fetch_timeout.total_seconds()
            try:
                detail = future.result(timeout=timeout_seconds)
            except FutureTimeoutError:
                future.cancel()
                raise TimeoutError(
                    f"fetch exceeded {timeout_seconds:.0f}s wall-clock budget"
                ) from None
            step.detail = detail
            step.succeeded = True
            logger.info(
                "Orchestrator[%s] fetch ok in %.2fs",
                self._market.exchange, time_module.monotonic() - t0,
            )
        except Exception as e:
            step.error_message = str(e)
            step.succeeded = False
            logger.warning(
                "Orchestrator[%s] fetch failed: %s",
                self._market.exchange, e,
                exc_info=True,
            )
            if allow_retry_schedule:
                self._schedule_fetch_retry()
                summary.retry_scheduled = True
        finally:
            step.duration_seconds = time_module.monotonic() - t0

    def _run_compute_step(self, summary: OrchestratorRunSummary) -> None:
        step = summary.compute
        t0 = time_module.monotonic()
        try:
            step.detail = self._compute_run_once()
            step.succeeded = True
        except Exception as e:
            step.error_message = str(e)
            step.succeeded = False
            logger.warning(
                "Orchestrator[%s] compute failed: %s",
                self._market.exchange, e,
                exc_info=True,
            )
        finally:
            step.duration_seconds = time_module.monotonic() - t0

    def _run_distribute_step(self, summary: OrchestratorRunSummary) -> None:
        step = summary.distribute
        t0 = time_module.monotonic()
        try:
            step.detail = self._distribute_run_once()
            step.succeeded = True
        except Exception as e:
            step.error_message = str(e)
            step.succeeded = False
            logger.warning(
                "Orchestrator[%s] distribute failed: %s",
                self._market.exchange, e,
                exc_info=True,
            )
        finally:
            step.duration_seconds = time_module.monotonic() - t0

    def _schedule_fetch_retry(self) -> None:
        """Queue a one-shot retry on the retry executor.

        Only one retry can be pending at a time per orchestrator. If
        somehow a second failure overlaps (admin run-now during a
        retry window), the second attempt's pending_retry stays None
        and the orchestrator's regular cadence handles the next try.
        """
        with self._pending_retry_lock:
            if self._pending_retry is not None and not self._pending_retry.done():
                logger.info(
                    "Orchestrator[%s] retry already pending; not stacking",
                    self._market.exchange,
                )
                return

            delay = self._settings.fetch_retry_delay.total_seconds()
            logger.info(
                "Orchestrator[%s] scheduling fetch retry in %.0fs",
                self._market.exchange, delay,
            )
            self._pending_retry = self._retry_executor.submit(
                self._retry_worker, delay,
            )

    def _retry_worker(self, delay_seconds: float) -> None:
        """Sleep `delay_seconds`, then run a single retry pass.

        Runs on the retry_executor's thread. Honors stop_event so a
        long retry-delay window doesn't block shutdown."""
        slept = 0.0
        while slept < delay_seconds and not self._stop_event.is_set():
            chunk = min(1.0, delay_seconds - slept)
            self._sleep_fn(chunk)
            slept += chunk
        if self._stop_event.is_set():
            logger.info(
                "Orchestrator[%s] retry cancelled by stop",
                self._market.exchange,
            )
            return
        try:
            self.run_once(is_retry=True)
        except Exception:
            # run_once() itself shouldn't raise (all its steps are
            # caught), but if anything escapes here, log and swallow —
            # the retry executor is a daemon and we don't want
            # exceptions to leak as scary tracebacks.
            logger.exception(
                "Orchestrator[%s] retry worker hit unexpected exception",
                self._market.exchange,
            )

    def _publish_pulse_safely(self, summary: OrchestratorRunSummary) -> None:
        """Publish to PulsePublisher with full exception isolation.

        If the pulse publisher misbehaves (Step 4's bug, or the GUI's
        SSE subscriber filling its buffer), we do NOT let that fail
        the orchestrator pipeline. Pulse is best-effort observability."""
        if self._pulse is None:
            return
        try:
            self._pulse.publish(summary)
        except Exception:
            logger.exception(
                "Orchestrator[%s] pulse.publish failed; suppressing",
                self._market.exchange,
            )

    def _loop(self) -> None:
        """The main loop. Runs on the dedicated daemon thread."""
        # Catch-up logic on startup.
        if self._settings.catch_up_on_startup and self._should_catch_up_now():
            logger.info(
                "Orchestrator[%s] catch-up fire on startup",
                self._market.exchange,
            )
            try:
                self.run_once()
            except Exception:
                logger.exception(
                    "Orchestrator[%s] catch-up fire raised",
                    self._market.exchange,
                )

        while not self._stop_event.is_set():
            try:
                next_tick = next_tick_after(
                    self._now_fn(), self._market, self._calendar,
                )
            except RuntimeError:
                # No trading day found in lookahead — calendar is
                # misconfigured. Log loudly, sleep a minute, retry.
                # We don't want to crash the loop on a transient
                # configuration error.
                logger.exception(
                    "Orchestrator[%s] next_tick_after failed; "
                    "sleeping 60s before retry",
                    self._market.exchange,
                )
                self._sleep_until_or_stop(self._now_fn() + timedelta(seconds=60))
                continue

            self._sleep_until_or_stop(next_tick)
            if self._stop_event.is_set():
                break
            try:
                self.run_once()
            except Exception:
                # As above: run_once() catches its own steps; an
                # exception here is a defect in run_once itself, not
                # a step failure. Log and continue the loop.
                logger.exception(
                    "Orchestrator[%s] unexpected exception in run_once",
                    self._market.exchange,
                )

    def _sleep_until_or_stop(self, target_utc: datetime) -> None:
        """Sleep in 1-second chunks until `target_utc` or stop_event."""
        while not self._stop_event.is_set():
            now = self._now_fn()
            remaining = (target_utc - now).total_seconds()
            if remaining <= 0:
                return
            self._sleep_fn(min(1.0, remaining))

    def _should_catch_up_now(self) -> bool:
        """Decide whether the startup catch-up fire should happen.

        True iff:
          - We are currently within a trading day's session window
            (after first scheduled tick of today, before the last), AND
          - last_run_completed_at is either None or older than
            catch_up_skip_if_recent.
        """
        now = self._now_fn()
        if self.last_run_completed_at is not None:
            elapsed = now - self.last_run_completed_at
            if elapsed < self._settings.catch_up_skip_if_recent:
                logger.info(
                    "Orchestrator[%s] skipping catch-up: last run %.0fs ago",
                    self._market.exchange, elapsed.total_seconds(),
                )
                return False
        today_local = now.astimezone(self._market.tz).date()
        if not self._calendar.is_trading_day(today_local):
            return False
        today_ticks = compute_tick_times_for_day(today_local, self._market)
        # Are we within the session? At least one tick has passed and
        # at least one tick is still ahead today.
        return any(t <= now for t in today_ticks) and any(
            t > now for t in today_ticks
        )


def _step_label(step: OrchestratorStepResult) -> str:
    """Short status string for log lines."""
    if step.skipped_reason is not None:
        return f"skipped({step.skipped_reason})"
    if step.succeeded:
        return f"ok({step.duration_seconds:.1f}s)"
    return f"failed({step.error_message})"
