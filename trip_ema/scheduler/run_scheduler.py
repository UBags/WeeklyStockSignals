"""RunScheduler — wall-clock tick planner thread.

Phase 3a addition. Reads market_hours config each iteration, computes
the next tick per exchange, sleeps until the earliest, and enqueues
(exchange, kind) into the RunQueue. RunDispatcher picks it up from
there.

Design contract (per operator spec):

  1. Config reread each iteration (Q9). Adding an exchange to
     config.yaml and restarting is NOT required — the next
     iteration picks up new entries. Removing an exchange takes
     effect immediately.

  2. Sleeps until the exact tick instant. On wake, enqueues,
     nudges 0.5s forward, restarts iteration. The 0.5s nudge
     prevents re-finding the same tick if the enqueue was
     instantaneous.

  3. Tick role -> kind mapping:
       'post_close' -> 'eod'
       everything else (post_open, mid_session, pre_close,
       legacy hourly, etc.) -> 'intraday'

  4. Interruptible sleep — the loop wakes every 1 second to check
     the stop event. On stop() the thread exits within ~1s of the
     next check.

  5. Gated by config.scheduling.dispatcher_enabled. When False
     (the Phase 3a default), the scheduler thread is constructed
     but never .start()'d. MarketOrchestrator remains in charge.
     When True, RunScheduler owns wall-clock scheduling and the
     operator must ALSO stop MarketOrchestrator (Phase 3b will
     delete that code path outright).

  6. Failure handling: any per-iteration exception is caught +
     logged and the loop continues. A single misconfigured
     exchange must not silently kill the entire scheduler.
"""
from __future__ import annotations

import logging
import threading
import time as time_module
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import Config
from ..config.loader import load_config
from .market_calendar import TradingCalendar
from .run_queue import RunQueue
from .tick_planner import (
    MarketTickConfig,
    compute_tick_entries_for_day,
    next_tick_after,
)


logger = logging.getLogger(__name__)


# Names in the Phase 16a tick_schedule that count as an "eod" run
# (per operator Q7 — filter emails per exchange after the post-close
# run). Everything else is "intraday". This tuple lives here so it's
# grep-able and unit-testable.
EOD_TICK_NAMES = ("post_close",)


@dataclass(frozen=True, slots=True)
class NextTick:
    """Result of `_find_earliest_next_tick`."""
    when_utc: datetime
    exchange: str
    name: str            # 'post_open', 'mid_session', 'pre_close', 'post_close', 'legacy'
    kind: str            # 'intraday' | 'eod'


class RunScheduler:
    """Long-running thread that computes and fires scheduled ticks
    into the RunQueue.

    Public API:
      - start() / stop()
      - is_alive() -> bool
      - next_tick_snapshot() -> NextTick | None
          Read-only view of what the scheduler is currently sleeping
          toward. Populated on each iteration. None between iterations
          or if config yields no ticks.

    Injected dependencies:
      - queue: RunQueue, required.
      - config_path: Path to config.yaml. Reread on every iteration.
      - config_reader: Callable[[Path], Config], defaults to
          config.loader.load_config. Overridden in tests.
      - calendar_factory: Callable[[str], TradingCalendar], required.
          The scheduler doesn't know how to build calendars — that's
          the caller's concern (production caller uses
          TradingCalendar(MarketHoursConfig.to_calendar_config())).
      - now_fn: Callable[[], datetime], defaults to `datetime.now(UTC)`.
          Overridden in tests for deterministic scheduling.
      - sleep_fn: Callable[[float], None], defaults to `time.sleep`.
      - min_sleep_seconds: float, minimum sleep chunk when waiting
          for tick. Keeps stop() responsive. Default 1.0.
    """

    def __init__(
        self, *,
        queue: RunQueue,
        config_path: Path,
        calendar_factory,
        config_reader=None,
        now_fn=None,
        sleep_fn=None,
        min_sleep_seconds: float = 1.0,
    ):
        self._queue = queue
        self._config_path = config_path
        self._calendar_factory = calendar_factory
        self._config_reader = config_reader or load_config
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._sleep = sleep_fn or time_module.sleep
        self._min_sleep = min_sleep_seconds

        self._stop_event = threading.Event()
        self._next_tick_snapshot: NextTick | None = None
        self._snapshot_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---------- thread lifecycle ---------- #

    def start(self) -> None:
        """Start the scheduler daemon thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            logger.info("RunScheduler already running; start() ignored")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="RunScheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("RunScheduler started")

    def stop(self) -> None:
        """Signal exit. The loop wakes at its next sleep boundary
        (up to `min_sleep_seconds` later)."""
        self._stop_event.set()
        logger.info("RunScheduler stop requested")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def next_tick_snapshot(self) -> NextTick | None:
        """Read-only snapshot of the tick currently being waited on."""
        with self._snapshot_lock:
            return self._next_tick_snapshot

    # ---------- main loop ---------- #

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._iterate_once()
            except Exception:
                logger.exception(
                    "RunScheduler iteration raised; continuing after brief pause",
                )
                # Back off briefly so we don't spin on a persistent config error.
                if not self._stop_event.wait(timeout=5.0):
                    continue
                break
        logger.info("RunScheduler loop exiting")

    def _iterate_once(self) -> None:
        # 1. Reread config so `market_hours` edits pick up without restart.
        try:
            config = self._config_reader(self._config_path)
        except Exception:
            logger.exception(
                "RunScheduler: failed to reload config from %s; "
                "sleeping 30s before retry",
                self._config_path,
            )
            self._stop_event.wait(timeout=30.0)
            return

        now = self._now()

        # 2. Compute earliest next tick across all configured exchanges.
        next_tick = self._find_earliest_next_tick(config, now)
        if next_tick is None:
            # No configured exchanges (or every one's config yielded
            # no future ticks — impossible in practice, but defensive).
            logger.warning(
                "RunScheduler: no next tick found across %d configured markets; "
                "sleeping 60s before recheck",
                len(config.market_hours),
            )
            self._stop_event.wait(timeout=60.0)
            return

        with self._snapshot_lock:
            self._next_tick_snapshot = next_tick

        # 3. Sleep in min_sleep_seconds chunks until the tick lands
        #    (or a stop is requested).
        logger.info(
            "RunScheduler: next tick at %s UTC (exchange=%s name=%s kind=%s)",
            next_tick.when_utc.isoformat(),
            next_tick.exchange, next_tick.name, next_tick.kind,
        )
        while True:
            if self._stop_event.is_set():
                return
            now = self._now()
            remaining = (next_tick.when_utc - now).total_seconds()
            if remaining <= 0:
                break
            self._sleep(min(self._min_sleep, remaining))

        # 4. Fire.
        result = self._queue.enqueue(
            next_tick.exchange, next_tick.kind,
        )
        logger.info(
            "RunScheduler: enqueued exchange=%s kind=%s result=%s",
            next_tick.exchange, next_tick.kind, result,
        )

        # 5. Nudge 0.5s past the tick so `next_tick_after(now, ...)`
        #    on the next iteration doesn't re-find the same instant.
        self._sleep(0.5)

    def _find_earliest_next_tick(
        self, config: Config, now: datetime,
    ) -> NextTick | None:
        """Iterate every configured market, compute its next tick,
        return the earliest.

        Silently skips markets whose tick computation raises — the
        loop's outer except would catch a crash here too, but
        per-market fault isolation keeps one broken config from
        stopping the scheduler for other markets."""
        best: NextTick | None = None

        for mh in config.market_hours:
            try:
                market: MarketTickConfig = mh.to_tick_config()
                calendar = self._calendar_factory(mh.exchange)
                next_utc = next_tick_after(now, market, calendar)
                name = _identify_tick_name(next_utc, market, calendar)
                kind = "eod" if name in EOD_TICK_NAMES else "intraday"
                candidate = NextTick(
                    when_utc=next_utc, exchange=mh.exchange,
                    name=name, kind=kind,
                )
                if best is None or candidate.when_utc < best.when_utc:
                    best = candidate
            except Exception:
                logger.exception(
                    "RunScheduler: tick computation failed for "
                    "exchange=%s; skipping this market this iteration",
                    mh.exchange,
                )
                continue

        return best


def _identify_tick_name(
    when_utc: datetime,
    market: MarketTickConfig,
    calendar: TradingCalendar,
) -> str:
    """Given a UTC tick instant we just computed via next_tick_after,
    look up which tick_schedule entry it corresponds to.

    Returns the entry's `name` field (e.g. 'post_open', 'post_close').
    Falls back to 'legacy' when the market doesn't have a
    tick_schedule (Mode B — the hourly cadence has no named roles).
    """
    from datetime import timedelta

    if market.tick_schedule is None:
        # Legacy Mode B — no roles, everything is intraday.
        return "legacy"

    # The tick instant was produced by compute_tick_times_for_day
    # on some local date. Try the local-tz date of `when_utc`, and
    # ±1 day either side to cover edge cases where post_close
    # crosses UTC midnight relative to market local.
    local_when = when_utc.astimezone(market.tz)
    for delta_days in (0, -1, 1):
        candidate_date = local_when.date() + timedelta(days=delta_days)
        entries = compute_tick_entries_for_day(candidate_date, market)
        for entry_time, entry_name in entries:
            if entry_time == when_utc:
                return entry_name

    # No match — shouldn't happen if tick_planner is internally
    # consistent. Log and default to 'legacy' so kind=='intraday'.
    logger.warning(
        "RunScheduler: could not identify tick name for %s at %s; "
        "defaulting to 'legacy' (kind will be intraday)",
        market.exchange, when_utc.isoformat(),
    )
    return "legacy"
