"""Tests for the tick planner.

Heavy on cases because this is the load-bearing pure function for
all market orchestration. Every edge case noted in the planner's
docstring has at least one corresponding test.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, UTC
from zoneinfo import ZoneInfo

import pytest

from trip_ema.scheduler.market_calendar import (
    TradingCalendar,
    TradingCalendarConfig,
)
from trip_ema.scheduler.tick_planner import (
    MarketTickConfig,
    compute_tick_times_for_day,
    next_tick_after,
)


IST = ZoneInfo("Asia/Kolkata")
UTC = UTC
EASTERN = ZoneInfo("America/New_York")


def _nse_market() -> MarketTickConfig:
    """Canonical NSE tick config: 09:15-15:30 IST, open+5, close-15, 1h."""
    return MarketTickConfig(
        exchange="NSE",
        tz=IST,
        open=time(9, 15),
        close=time(15, 30),
        post_open_offset=timedelta(minutes=5),
        pre_close_offset=timedelta(minutes=15),
        intraday_cadence=timedelta(hours=1),
    )


def _nse_calendar() -> TradingCalendar:
    """NSE-style calendar: Mon-Fri, no holidays (so tests don't depend
    on python-holidays' date list)."""
    return TradingCalendar(
        TradingCalendarConfig(
            exchange="NSE",
            weekdays=frozenset([1, 2, 3, 4, 5]),
        )
    )


# ---------------------------------------------------------------------------
# compute_tick_times_for_day: shape of one day's schedule
# ---------------------------------------------------------------------------


class TestComputeTickTimesForDay:
    def test_nse_canonical_schedule(self):
        # 2026-05-19 is a Tuesday. Expected ticks (IST):
        # 09:20, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 15:15.
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nse_market())
        # Convert back to IST for readable assertions.
        ticks_ist = [t.astimezone(IST).strftime("%H:%M") for t in ticks]
        assert ticks_ist == [
            "09:20", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "15:15",
        ]

    def test_nse_schedule_count(self):
        # Eight ticks per NSE trading day. This is a regression guard:
        # if someone ever changes the canonical offsets and the count
        # shifts, the test fails loudly.
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nse_market())
        assert len(ticks) == 8

    def test_all_ticks_are_utc(self):
        # The planner promises UTC instants.
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nse_market())
        for t in ticks:
            assert t.tzinfo == UTC, f"Tick {t} is not in UTC"

    def test_ticks_are_strictly_ascending(self):
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nse_market())
        for prev, nxt in zip(ticks, ticks[1:]):
            assert prev < nxt, f"Ticks not ascending: {prev} >= {nxt}"

    def test_no_intraday_ticks_when_window_is_too_short(self):
        # Market open 09:00, close 09:30. post_open=5min, pre_close=15min.
        # post_open = 09:05, pre_close = 09:15. Hourly cadence boundaries
        # between them: none (10:00 is past pre_close). So just open and
        # pre_close (but pre_close > post_open, so both included).
        m = MarketTickConfig(
            exchange="SHORT",
            tz=IST,
            open=time(9, 0),
            close=time(9, 30),
            post_open_offset=timedelta(minutes=5),
            pre_close_offset=timedelta(minutes=15),
            intraday_cadence=timedelta(hours=1),
        )
        ticks = compute_tick_times_for_day(date(2026, 5, 19), m)
        labels = [t.astimezone(IST).strftime("%H:%M") for t in ticks]
        assert labels == ["09:05", "09:15"]

    def test_pre_close_skipped_if_before_post_open(self):
        # Misconfigured: open 09:00, close 09:10, post_open 5, pre_close 15.
        # post_open=09:05, pre_close=08:55 (close - 15 = 09:10 - 0:15).
        # pre_close < post_open → only post_open tick fires.
        m = MarketTickConfig(
            exchange="BROKEN",
            tz=IST,
            open=time(9, 0),
            close=time(9, 10),
            post_open_offset=timedelta(minutes=5),
            pre_close_offset=timedelta(minutes=15),
            intraday_cadence=timedelta(hours=1),
        )
        ticks = compute_tick_times_for_day(date(2026, 5, 19), m)
        labels = [t.astimezone(IST).strftime("%H:%M") for t in ticks]
        assert labels == ["09:05"]

    def test_post_open_past_close_yields_empty(self):
        # post_open=09:35, close=09:30. Whole day is non-firing.
        m = MarketTickConfig(
            exchange="BAD",
            tz=IST,
            open=time(9, 0),
            close=time(9, 30),
            post_open_offset=timedelta(minutes=35),
            pre_close_offset=timedelta(minutes=5),
            intraday_cadence=timedelta(hours=1),
        )
        ticks = compute_tick_times_for_day(date(2026, 5, 19), m)
        assert ticks == []

    def test_zero_post_open_offset(self):
        # post_open == open. First tick is exactly at open.
        m = MarketTickConfig(
            exchange="NOOFFSET",
            tz=IST,
            open=time(9, 0),
            close=time(15, 30),
            post_open_offset=timedelta(0),
            pre_close_offset=timedelta(minutes=15),
            intraday_cadence=timedelta(hours=1),
        )
        ticks = compute_tick_times_for_day(date(2026, 5, 19), m)
        labels = [t.astimezone(IST).strftime("%H:%M") for t in ticks]
        # 09:00 (open), then 10:00 (first boundary strictly after 09:00),
        # 11..15:00, then 15:15 (close - 15).
        assert labels[0] == "09:00"
        assert labels[1] == "10:00"
        assert labels[-1] == "15:15"

    def test_half_hour_cadence(self):
        # Same NSE window but with 30-min cadence. Should emit :00 and
        # :30 boundaries.
        m = MarketTickConfig(
            exchange="HALFH",
            tz=IST,
            open=time(9, 15),
            close=time(15, 30),
            post_open_offset=timedelta(minutes=5),  # 09:20
            pre_close_offset=timedelta(minutes=15),  # 15:15
            intraday_cadence=timedelta(minutes=30),
        )
        ticks = compute_tick_times_for_day(date(2026, 5, 19), m)
        labels = [t.astimezone(IST).strftime("%H:%M") for t in ticks]
        # 09:20, then 09:30, 10:00, 10:30, ..., 15:00, then 15:15.
        assert labels[0] == "09:20"
        assert labels[1] == "09:30"
        assert labels[2] == "10:00"
        assert labels[-1] == "15:15"

    def test_invalid_zero_cadence_raises(self):
        m = MarketTickConfig(
            exchange="ZERO",
            tz=IST,
            open=time(9, 15),
            close=time(15, 30),
            post_open_offset=timedelta(minutes=5),
            pre_close_offset=timedelta(minutes=15),
            intraday_cadence=timedelta(0),
        )
        with pytest.raises(ValueError, match="positive"):
            compute_tick_times_for_day(date(2026, 5, 19), m)

    def test_pre_close_dedup_when_lands_on_boundary(self):
        # Engineer a case where close-pre_close == a top-of-hour.
        # close=15:00, pre_close_offset=0 → pre_close=15:00 (a boundary).
        # post_open=09:20 → boundaries 10:00..14:00 emit hourly.
        # The pre_close=15:00 tick should be deduplicated against the
        # 15:00 hourly boundary (which would also be emitted if 15:00
        # < pre_close, but it equals pre_close, so the boundary loop
        # excludes it via "<" comparison).
        m = MarketTickConfig(
            exchange="DEDUP",
            tz=IST,
            open=time(9, 15),
            close=time(15, 0),
            post_open_offset=timedelta(minutes=5),
            pre_close_offset=timedelta(0),
            intraday_cadence=timedelta(hours=1),
        )
        ticks = compute_tick_times_for_day(date(2026, 5, 19), m)
        labels = [t.astimezone(IST).strftime("%H:%M") for t in ticks]
        # 09:20, 10..14:00 hourly (NOT 15:00 from boundary loop, since
        # the loop is "< pre_close" strict), 15:00 from pre_close. So
        # we get one 15:00, not two.
        assert labels.count("15:00") == 1


# ---------------------------------------------------------------------------
# next_tick_after: finding the *next* tick from an arbitrary instant
# ---------------------------------------------------------------------------


class TestNextTickAfter:
    def test_before_open_yields_post_open(self):
        # 08:30 IST Tuesday → first tick of today is 09:20 IST.
        now = datetime(2026, 5, 19, 8, 30, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        assert result.astimezone(IST) == datetime(2026, 5, 19, 9, 20, 0, tzinfo=IST)

    def test_between_open_and_post_open_yields_post_open(self):
        # 09:18 IST → still before 09:20.
        now = datetime(2026, 5, 19, 9, 18, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        assert result.astimezone(IST) == datetime(2026, 5, 19, 9, 20, 0, tzinfo=IST)

    def test_mid_session_yields_next_hourly(self):
        # 11:47 IST → next tick is 12:00.
        now = datetime(2026, 5, 19, 11, 47, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        assert result.astimezone(IST) == datetime(2026, 5, 19, 12, 0, 0, tzinfo=IST)

    def test_just_before_pre_close_yields_pre_close(self):
        # 15:10 IST → next is 15:15.
        now = datetime(2026, 5, 19, 15, 10, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        assert result.astimezone(IST) == datetime(2026, 5, 19, 15, 15, 0, tzinfo=IST)

    def test_at_pre_close_yields_next_trading_day(self):
        # 15:15:00 IST exactly. Strict "> now" — next tick is tomorrow.
        now = datetime(2026, 5, 19, 15, 15, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        # 2026-05-20 is Wednesday, so the next tick is Wed 09:20 IST.
        assert result.astimezone(IST) == datetime(2026, 5, 20, 9, 20, 0, tzinfo=IST)

    def test_after_pre_close_yields_next_trading_day(self):
        # 18:30 IST Tuesday → next is Wed 09:20.
        now = datetime(2026, 5, 19, 18, 30, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        assert result.astimezone(IST) == datetime(2026, 5, 20, 9, 20, 0, tzinfo=IST)

    def test_friday_post_close_skips_weekend(self):
        # Friday 2026-05-22 17:00 IST → next is Monday 2026-05-25 09:20.
        now = datetime(2026, 5, 22, 17, 0, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        assert result.astimezone(IST) == datetime(2026, 5, 25, 9, 20, 0, tzinfo=IST)

    def test_saturday_returns_monday(self):
        now = datetime(2026, 5, 23, 11, 0, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        assert result.astimezone(IST) == datetime(2026, 5, 25, 9, 20, 0, tzinfo=IST)

    def test_sunday_returns_monday(self):
        now = datetime(2026, 5, 24, 23, 0, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        assert result.astimezone(IST) == datetime(2026, 5, 25, 9, 20, 0, tzinfo=IST)

    def test_holiday_in_middle_of_week_skipped(self):
        # Mark Tuesday 2026-05-19 as a holiday via override.
        import tempfile
        from pathlib import Path
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("extra_holidays: [2026-05-19]")
            override_path = Path(f.name)
        try:
            cal = TradingCalendar(
                TradingCalendarConfig(
                    exchange="NSE",
                    weekdays=frozenset([1, 2, 3, 4, 5]),
                    holidays_override_file=override_path,
                )
            )
            # Monday 2026-05-18 17:00 IST. Next non-holiday trading day
            # is Wednesday 2026-05-20.
            now = datetime(2026, 5, 18, 17, 0, 0, tzinfo=IST)
            result = next_tick_after(now, _nse_market(), cal)
            assert result.astimezone(IST) == datetime(
                2026, 5, 20, 9, 20, 0, tzinfo=IST
            )
        finally:
            override_path.unlink()

    def test_input_in_utc_handled_correctly(self):
        # 06:17 UTC = 11:47 IST → next tick is 12:00 IST.
        now_utc = datetime(2026, 5, 19, 6, 17, 0, tzinfo=UTC)
        result = next_tick_after(now_utc, _nse_market(), _nse_calendar())
        assert result.astimezone(IST) == datetime(2026, 5, 19, 12, 0, 0, tzinfo=IST)

    def test_returns_utc_aware_datetime(self):
        now = datetime(2026, 5, 19, 11, 0, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        assert result.tzinfo == UTC

    def test_naive_now_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            next_tick_after(
                datetime(2026, 5, 19, 11, 0, 0),
                _nse_market(),
                _nse_calendar(),
            )

    def test_raises_after_max_lookahead_exhausted(self):
        # Calendar that says nothing is ever a trading day.
        cal = TradingCalendar(
            TradingCalendarConfig(
                exchange="NEVER",
                weekdays=frozenset(),
            )
        )
        now = datetime(2026, 5, 19, 11, 0, 0, tzinfo=IST)
        with pytest.raises(RuntimeError, match="No tick found"):
            next_tick_after(now, _nse_market(), cal, max_lookahead_days=5)

    def test_first_tick_of_day_exactly_returns_next(self):
        # If now == today's 09:20 IST exactly, strict ">" means we go
        # to the next tick (10:00), not today's 09:20.
        now = datetime(2026, 5, 19, 9, 20, 0, tzinfo=IST)
        result = next_tick_after(now, _nse_market(), _nse_calendar())
        assert result.astimezone(IST) == datetime(2026, 5, 19, 10, 0, 0, tzinfo=IST)


# ---------------------------------------------------------------------------
# DST: USA spring-forward (Eastern), as a generality check
# ---------------------------------------------------------------------------


class TestDstHandling:
    def _us_market(self) -> MarketTickConfig:
        # NYSE-like: 09:30-16:00 ET, open+5 = 09:35, close-15 = 15:45,
        # hourly cadence.
        return MarketTickConfig(
            exchange="NYSE",
            tz=EASTERN,
            open=time(9, 30),
            close=time(16, 0),
            post_open_offset=timedelta(minutes=5),
            pre_close_offset=timedelta(minutes=15),
            intraday_cadence=timedelta(hours=1),
        )

    def _us_calendar(self) -> TradingCalendar:
        return TradingCalendar(
            TradingCalendarConfig(
                exchange="NYSE",
                weekdays=frozenset([1, 2, 3, 4, 5]),
            )
        )

    def test_spring_forward_sunday_skipped(self):
        # 2026-03-08 is a Sunday — DST starts. Markets closed weekends
        # anyway; the next trading day is Monday 2026-03-09 09:35 ET.
        # We're verifying that the planner doesn't trip on the
        # transition while walking past it.
        now = datetime(2026, 3, 8, 1, 0, 0, tzinfo=EASTERN)
        result = next_tick_after(now, self._us_market(), self._us_calendar())
        # The result IS Monday 09:35 ET. EDT (offset -04:00) is now in
        # effect, so 09:35 EDT = 13:35 UTC.
        assert result.astimezone(EASTERN) == datetime(
            2026, 3, 9, 9, 35, 0, tzinfo=EASTERN
        )

    def test_canonical_us_schedule(self):
        # 2026-03-10 (Tuesday, in EDT). Expected ticks: 09:35, 10:00,
        # 11:00, 12:00, 13:00, 14:00, 15:00, 15:45.
        ticks = compute_tick_times_for_day(date(2026, 3, 10), self._us_market())
        labels = [t.astimezone(EASTERN).strftime("%H:%M") for t in ticks]
        assert labels == [
            "09:35", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "15:45",
        ]
