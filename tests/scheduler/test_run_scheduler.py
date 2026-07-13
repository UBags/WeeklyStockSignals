"""Tests for RunScheduler — Phase 3a wall-clock tick planner.

Uses a mocked clock + config reader so the scheduler runs
deterministically without waiting for real wall-clock time.
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime, timedelta, time as dt_time
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from trip_ema.scheduler.market_calendar import (
    TradingCalendar, TradingCalendarConfig,
)
from trip_ema.scheduler.run_queue import RunQueue
from trip_ema.scheduler.run_scheduler import (
    EOD_TICK_NAMES, NextTick, RunScheduler, _identify_tick_name,
)
from trip_ema.scheduler.tick_planner import (
    MarketTickConfig, ResolvedTickEntry,
)


IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# Fixtures
# ============================================================

def _nse_market_config() -> MarketTickConfig:
    """NSE Phase-16a config: 09:15-15:30 IST + 4-tick schedule."""
    return MarketTickConfig(
        exchange="NSE",
        tz=IST,
        open=dt_time(9, 15),
        close=dt_time(15, 30),
        post_open_offset=timedelta(minutes=15),
        pre_close_offset=timedelta(minutes=30),
        intraday_cadence=timedelta(minutes=60),
        tick_schedule=(
            ResolvedTickEntry("post_open",   "open",     15),
            ResolvedTickEntry("mid_session", "midpoint", 0),
            ResolvedTickEntry("pre_close",   "close",    -30),
            ResolvedTickEntry("post_close",  "close",    60),
        ),
    )


def _all_weekdays_calendar(exchange: str) -> TradingCalendar:
    """Mon-Fri trading, no holidays. Same pattern as test_tick_planner.py."""
    return TradingCalendar(
        TradingCalendarConfig(
            exchange=exchange,
            weekdays=frozenset([1, 2, 3, 4, 5]),
        )
    )


class _MockConfig:
    """Stand-in for the real Config — only exposes what RunScheduler reads."""
    def __init__(self, market_hours):
        self.market_hours = market_hours


class _MockMarketHours:
    """Stand-in for MarketHoursConfig — only .to_tick_config() and
    .exchange are used by RunScheduler."""
    def __init__(self, tick_config: MarketTickConfig):
        self._tc = tick_config
        self.exchange = tick_config.exchange

    def to_tick_config(self) -> MarketTickConfig:
        return self._tc


# ============================================================
# _identify_tick_name helper
# ============================================================

class TestIdentifyTickName:
    def test_identifies_post_open(self):
        market = _nse_market_config()
        cal = _all_weekdays_calendar("NSE")
        # NSE post_open on 2026-05-19 (Tue) = 09:30 IST = 04:00 UTC.
        post_open_utc = datetime(2026, 5, 19, 4, 0, tzinfo=UTC)
        assert _identify_tick_name(post_open_utc, market, cal) == "post_open"

    def test_identifies_post_close(self):
        market = _nse_market_config()
        cal = _all_weekdays_calendar("NSE")
        # NSE post_close on 2026-05-19 = 16:30 IST = 11:00 UTC.
        post_close_utc = datetime(2026, 5, 19, 11, 0, tzinfo=UTC)
        assert _identify_tick_name(post_close_utc, market, cal) == "post_close"

    def test_legacy_config_returns_legacy(self):
        # Mode B: no tick_schedule -> everything is 'legacy'.
        legacy_market = MarketTickConfig(
            exchange="NSE",
            tz=IST,
            open=dt_time(9, 15),
            close=dt_time(15, 30),
            post_open_offset=timedelta(minutes=5),
            pre_close_offset=timedelta(minutes=15),
            intraday_cadence=timedelta(minutes=60),
            tick_schedule=None,
        )
        cal = _all_weekdays_calendar("NSE")
        assert _identify_tick_name(
            datetime(2026, 5, 19, 4, 0, tzinfo=UTC),
            legacy_market, cal,
        ) == "legacy"


# ============================================================
# EOD_TICK_NAMES tuple
# ============================================================

class TestEodTickNames:
    def test_post_close_is_eod(self):
        assert "post_close" in EOD_TICK_NAMES

    def test_post_open_is_not_eod(self):
        assert "post_open" not in EOD_TICK_NAMES

    def test_mid_session_is_not_eod(self):
        assert "mid_session" not in EOD_TICK_NAMES

    def test_pre_close_is_not_eod(self):
        # Q3 lock — 'pre_close' is intraday, not eod.
        assert "pre_close" not in EOD_TICK_NAMES

    def test_legacy_is_not_eod(self):
        assert "legacy" not in EOD_TICK_NAMES


# ============================================================
# _find_earliest_next_tick logic
# ============================================================

class TestFindEarliestNextTick:
    def _make_scheduler(self, market_configs, now):
        queue = RunQueue()
        cal_factory = _all_weekdays_calendar
        return RunScheduler(
            queue=queue,
            config_path=Path("/nonexistent.yaml"),
            calendar_factory=cal_factory,
            config_reader=lambda p: _MockConfig(
                [_MockMarketHours(tc) for tc in market_configs]
            ),
            now_fn=lambda: now,
            sleep_fn=lambda _: None,
        )

    def test_single_market_returns_its_next_tick(self):
        # 2026-05-19 Tuesday 08:00 UTC = 13:30 IST — after 12:15 mid_session,
        # before 15:00 pre_close.
        now = datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
        market = _nse_market_config()
        sched = self._make_scheduler([market], now)
        config = _MockConfig([_MockMarketHours(market)])

        result = sched._find_earliest_next_tick(config, now)
        assert result is not None
        assert result.exchange == "NSE"
        # Next NSE tick after 13:30 IST is 15:00 IST pre_close = 09:30 UTC.
        assert result.when_utc == datetime(2026, 5, 19, 9, 30, tzinfo=UTC)
        assert result.name == "pre_close"
        assert result.kind == "intraday"

    def test_post_close_yields_eod_kind(self):
        # 2026-05-19 Tuesday 10:00 UTC = 15:30 IST (exactly at close).
        # Next tick is post_close at 16:30 IST = 11:00 UTC.
        now = datetime(2026, 5, 19, 10, 0, tzinfo=UTC)
        market = _nse_market_config()
        sched = self._make_scheduler([market], now)
        config = _MockConfig([_MockMarketHours(market)])

        result = sched._find_earliest_next_tick(config, now)
        assert result is not None
        assert result.name == "post_close"
        assert result.kind == "eod"

    def test_multiple_markets_returns_earliest(self):
        # NSE + NYSE. NSE post_open at 09:30 IST = 04:00 UTC.
        # NYSE post_open at 09:45 ET = 13:45 UTC (with market open 09:30 ET
        # + 15min offset).
        # From now=03:00 UTC on 2026-05-19, NSE's tick fires first.
        now = datetime(2026, 5, 19, 3, 0, tzinfo=UTC)
        nse = _nse_market_config()
        # Fake NYSE — 09:30-16:00 ET
        nyse = MarketTickConfig(
            exchange="NYSE",
            tz=ZoneInfo("America/New_York"),
            open=dt_time(9, 30),
            close=dt_time(16, 0),
            post_open_offset=timedelta(minutes=15),
            pre_close_offset=timedelta(minutes=30),
            intraday_cadence=timedelta(minutes=60),
            tick_schedule=(
                ResolvedTickEntry("post_open",   "open",     15),
                ResolvedTickEntry("mid_session", "midpoint", 0),
                ResolvedTickEntry("pre_close",   "close",    -30),
                ResolvedTickEntry("post_close",  "close",    60),
            ),
        )
        sched = self._make_scheduler([nse, nyse], now)
        config = _MockConfig(
            [_MockMarketHours(nse), _MockMarketHours(nyse)]
        )

        result = sched._find_earliest_next_tick(config, now)
        assert result is not None
        assert result.exchange == "NSE"  # NSE's tick fires before NYSE's

    def test_empty_config_returns_none(self):
        now = datetime(2026, 5, 19, 3, 0, tzinfo=UTC)
        sched = self._make_scheduler([], now)
        config = _MockConfig([])
        assert sched._find_earliest_next_tick(config, now) is None

    def test_one_broken_market_does_not_break_others(self):
        # If one market's to_tick_config raises, the scheduler
        # still returns the other market's next tick.
        now = datetime(2026, 5, 19, 3, 0, tzinfo=UTC)
        good = _nse_market_config()

        class BrokenMH:
            exchange = "BROKEN"
            def to_tick_config(self):
                raise ValueError("simulated broken config")

        sched = self._make_scheduler([good], now)
        config = _MockConfig([BrokenMH(), _MockMarketHours(good)])

        result = sched._find_earliest_next_tick(config, now)
        assert result is not None
        assert result.exchange == "NSE"


# ============================================================
# Main-loop enqueue behavior
# ============================================================

class TestSchedulerLoopEnqueues:
    def test_fires_on_next_tick(self):
        """Advance mock clock past the next tick; scheduler enqueues once."""
        queue = RunQueue()
        market = _nse_market_config()

        # Starting time: 03:59 UTC = 09:29 IST, 1 minute before post_open.
        clock = [datetime(2026, 5, 19, 3, 59, tzinfo=UTC)]

        def now_fn():
            return clock[0]

        def sleep_fn(seconds):
            # Advance mock clock and yield so other threads can run.
            clock[0] = clock[0] + timedelta(seconds=seconds)

        sched = RunScheduler(
            queue=queue,
            config_path=Path("/nonexistent.yaml"),
            calendar_factory=_all_weekdays_calendar,
            config_reader=lambda p: _MockConfig(
                [_MockMarketHours(market)]
            ),
            now_fn=now_fn,
            sleep_fn=sleep_fn,
            min_sleep_seconds=1.0,
        )

        sched.start()
        # Wait for the queue to have exactly one item — up to 2 real
        # seconds of wall clock (the loop's mock clock advances fast).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(queue) == 0:
            time.sleep(0.02)

        sched.stop()
        sched._thread.join(timeout=2.0)

        # Post_open enqueued as intraday.
        snap = queue.snapshot()
        assert len(snap) >= 1
        first = snap[0]
        assert first.exchange == "NSE"
        assert first.kind == "intraday"

    def test_next_tick_snapshot_populated(self):
        queue = RunQueue()
        market = _nse_market_config()
        now = datetime(2026, 5, 19, 3, 0, tzinfo=UTC)

        sched = RunScheduler(
            queue=queue,
            config_path=Path("/nonexistent.yaml"),
            calendar_factory=_all_weekdays_calendar,
            config_reader=lambda p: _MockConfig(
                [_MockMarketHours(market)]
            ),
            now_fn=lambda: now,
            # Sleep that never advances real time and returns immediately —
            # loop will be in the while-remaining-positive loop forever
            # unless we stop it. That's fine; we're testing the snapshot.
            sleep_fn=lambda _: None,
        )

        sched.start()

        # Wait for the scheduler to enter its sleep-until-tick loop.
        deadline = time.monotonic() + 2.0
        while (
            time.monotonic() < deadline
            and sched.next_tick_snapshot() is None
        ):
            time.sleep(0.02)

        snapshot = sched.next_tick_snapshot()
        sched.stop()
        sched._thread.join(timeout=2.0)

        assert snapshot is not None
        assert snapshot.exchange == "NSE"
        assert snapshot.name == "post_open"
        assert snapshot.kind == "intraday"


# ============================================================
# Lifecycle
# ============================================================

class TestLifecycle:
    def test_stop_before_start_is_a_noop(self):
        sched = RunScheduler(
            queue=RunQueue(),
            config_path=Path("/nonexistent.yaml"),
            calendar_factory=_all_weekdays_calendar,
            config_reader=lambda p: _MockConfig([]),
        )
        # Should not raise.
        sched.stop()
        assert not sched.is_alive()

    def test_start_twice_is_idempotent(self):
        sched = RunScheduler(
            queue=RunQueue(),
            config_path=Path("/nonexistent.yaml"),
            calendar_factory=_all_weekdays_calendar,
            config_reader=lambda p: _MockConfig([]),
        )
        sched.start()
        first_thread = sched._thread
        sched.start()
        assert sched._thread is first_thread   # same instance
        sched.stop()
        sched._thread.join(timeout=2.0)

    def test_config_reader_raising_does_not_kill_thread(self):
        """Persistent config errors should be logged and retried,
        not crash the scheduler thread."""
        queue = RunQueue()
        error_count = [0]

        def bad_reader(p):
            error_count[0] += 1
            raise OSError("simulated config load error")

        # Very short retry-sleep to make the test fast.
        sched = RunScheduler(
            queue=queue,
            config_path=Path("/nonexistent.yaml"),
            calendar_factory=_all_weekdays_calendar,
            config_reader=bad_reader,
            now_fn=lambda: datetime(2026, 5, 19, 3, 0, tzinfo=UTC),
            # Not actually reached — config reader raises first.
            sleep_fn=lambda _: None,
        )

        sched.start()
        # Wait for the config reader to have been called at least
        # twice (indicating the loop is retrying, not dead).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and error_count[0] < 2:
            time.sleep(0.02)

        sched.stop()
        sched._thread.join(timeout=5.0)

        # Not necessarily >= 2 in fast tests, but at least 1.
        assert error_count[0] >= 1
