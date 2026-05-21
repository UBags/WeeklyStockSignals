"""Quarterly fundamentals refresh scheduler (Phase 1).

A background thread that wakes once per day, checks the calendar, and
fires `run_industry_refresh()` for each configured exchange when:

  - The current date is on or after the quarter-end + 1 month
    "filing window": 30 April, 31 July, 31 October, 31 January (in IST).
  - AND the last SUCCESS row for (industry, exchange) is older than
    80 days (or absent entirely). The 80-day window means we won't
    fire twice in the same filing season if the scheduler is restarted.

Why a separate thread and not piggybacking on MarketOrchestrator:

  - The orchestrator is tied to market sessions and per-ticker
    compute. The quarterly refresh is on a totally different cadence
    (4× a year vs. continuous) and takes 30+ minutes per exchange
    — long enough that running it inside an orchestrator tick would
    skew the cadence of everything else.
  - Operators can still trigger refreshes any time via CLI or HTTP;
    this scheduler is just the "set it and forget it" path.
  - Trivially disable-able: don't start the thread.

Tests pass `wakeup_seconds` to make the loop tight enough to exercise
without waiting 24 hours.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Engine

from ..config import Config
from ..etl.exchanges import UnsupportedExchangeError, configured_exchanges
from ..persistence.fundamentals_repo import FundamentalsRunRepository
from .runners import run_industry_refresh

logger = logging.getLogger(__name__)


# Phase 1: only the industry phase is scheduled. Phases 2/3 will add
# their own scheduler hooks once their runners exist.
SCHEDULED_PHASES = ("industry",)

# Refresh-eligible filing window: quarter-end + 1 month. We use these
# (month, day) tuples as the "from" cutoff — once the IST date is on
# or after one of them, we're in the refresh window for THIS quarter.
QUARTERLY_WINDOWS = [
    (1, 31),   # Q3 (Oct-Dec) results filed by end-January
    (4, 30),   # Q4 (Jan-Mar) results filed by end-April
    (7, 31),   # Q1 (Apr-Jun) results filed by end-July
    (10, 31),  # Q2 (Jul-Sep) results filed by end-October
]

# "Recent enough" threshold — if a SUCCESS run is newer than this many
# days, the scheduler considers this quarter handled and skips. 80
# days is "less than a quarter, more than the longest filing window
# we'd want a buffer for".
RECENT_RUN_DAYS = 80


class QuarterlyFundamentalsScheduler:
    """Background thread that fires fundamentals refreshes once per
    quarter, per configured exchange.

    Lifecycle:
        scheduler = QuarterlyFundamentalsScheduler(engine, config)
        scheduler.start()    # spawns the thread
        ...
        scheduler.stop()     # signals the thread to exit, joins it

    Both start() and stop() are idempotent.
    """

    def __init__(
        self,
        db_engine: Engine,
        config: Config,
        *,
        wakeup_seconds: float = 3600.0,  # 1 hour
        now_fn=None,
    ):
        self._db_engine = db_engine
        self._config = config
        self._wakeup_seconds = wakeup_seconds
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

    # ----- lifecycle ----- #

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="QuarterlyFundamentalsScheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ----- main loop ----- #

    def _loop(self) -> None:
        """Wake every wakeup_seconds, check whether any refreshes
        are due, fire them serially (one exchange at a time)."""
        logger.info(
            "QuarterlyFundamentalsScheduler started (wakeup=%.1fs)",
            self._wakeup_seconds,
        )
        while not self._stop_evt.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("QuarterlyFundamentalsScheduler tick failed")
            # Sleep on the event, not time.sleep — lets stop() wake us.
            self._stop_evt.wait(self._wakeup_seconds)
        logger.info("QuarterlyFundamentalsScheduler stopped")

    def _tick(self) -> None:
        """One iteration of the scheduler loop. Public for testability."""
        if not self._in_refresh_window():
            return
        exchanges = sorted(configured_exchanges(self._config))
        for exchange in exchanges:
            if self._stop_evt.is_set():
                return
            for phase in SCHEDULED_PHASES:
                if self._stop_evt.is_set():
                    return
                if self._skip_due_to_recent_run(phase, exchange):
                    logger.debug(
                        "Scheduler: skipping %s/%s (recent SUCCESS run exists)",
                        phase, exchange,
                    )
                    continue
                self._fire(phase, exchange)

    # ----- decisions ----- #

    def _in_refresh_window(self) -> bool:
        """Are we within ANY of the quarterly windows (i.e. within
        the 60 days following a quarter-end-plus-one-month)?

        The window is 60 days wide so that operators have a real
        buffer if the scheduler is down at the precise day.
        """
        today = self._now_fn().date()
        for month, day in QUARTERLY_WINDOWS:
            window_start = date(today.year, month, day)
            window_end_julian = window_start.toordinal() + 60
            window_end = date.fromordinal(window_end_julian)
            if window_start <= today <= window_end:
                return True
        return False

    def _skip_due_to_recent_run(self, phase: str, exchange: str) -> bool:
        try:
            with self._db_engine.connect() as conn:
                last = FundamentalsRunRepository(conn).last_success(
                    phase=phase, exchange=exchange,
                )
        except Exception:
            logger.exception(
                "Scheduler: failed to read last_success for %s/%s",
                phase, exchange,
            )
            # On error, err on the side of NOT firing — we don't want
            # a transient DB blip to trigger a 30-minute refresh.
            return True
        if last is None or last.completed_at is None:
            return False
        age_days = (self._now_fn() - last.completed_at).days
        return age_days < RECENT_RUN_DAYS

    def _fire(self, phase: str, exchange: str) -> None:
        if phase != "industry":
            # Phase 1 only knows about industry.
            return
        logger.info(
            "Scheduler: firing industry refresh for %s",
            exchange,
        )
        try:
            run_industry_refresh(self._db_engine, exchange)
        except UnsupportedExchangeError:
            logger.warning(
                "Scheduler: exchange %s not supported by codebase; skipping",
                exchange,
            )
        except Exception:
            logger.exception(
                "Scheduler: industry refresh failed for %s",
                exchange,
            )
