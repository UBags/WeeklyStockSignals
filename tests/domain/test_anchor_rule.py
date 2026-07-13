"""The anchor rule, asserted on both sides of UTC.

Every timezone bug this codebase has had shares one shape: an assumption that was
true for Asia/Kolkata. So every property here is checked on NSE *and* on NYSE —
one zone east of UTC, one west — because a test that only exercises the eastern
case would have passed against the bug it exists to prevent.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trip_ema.domain.market_session import (
    bar_anchor,
    day_bounds,
    market_tz,
    monday_instant_of,
    monday_of,
    range_bounds,
    trading_date_of,
    week_anchor,
)
from trip_ema.indicators.aggregation import _monday_of


class TestRoundTrip:
    """trading_date_of(bar_anchor(d)) == d. The whole rule, in one line."""

    @pytest.mark.parametrize("exchange", ["NSE", "NYSE"])
    def test_every_day_of_a_year_round_trips(self, exchange):
        d = date(2025, 1, 1)
        while d < date(2026, 1, 1):
            assert trading_date_of(bar_anchor(d, exchange), exchange) == d
            d += timedelta(days=1)

    def test_the_old_convention_does_not_round_trip_in_the_west(self):
        """The bug, stated as a fact. If this ever fails, the premise is gone."""
        monday = date(2026, 7, 13)
        assert monday.weekday() == 0
        old = datetime.combine(monday, datetime.min.time(), tzinfo=UTC)
        assert old.astimezone(market_tz("NYSE")).date() == date(2026, 7, 12)
        assert old.astimezone(market_tz("NSE")).date() == monday


class TestWeekAnchor:

    @pytest.mark.parametrize("exchange", ["NSE", "NYSE"])
    def test_monday_bar_belongs_to_its_own_week(self, exchange):
        """The failure mode: a Monday bar bucketing into the PREVIOUS week."""
        monday = date(2026, 7, 13)
        anchor = bar_anchor(monday, exchange)
        assert monday_instant_of(anchor, market_tz(exchange)) == week_anchor(
            monday, exchange,
        )
        assert trading_date_of(week_anchor(monday, exchange), exchange) == monday

    @pytest.mark.parametrize("exchange", ["NSE", "NYSE"])
    def test_every_day_of_a_week_shares_one_anchor(self, exchange):
        monday = date(2026, 7, 13)
        anchors = {
            monday_instant_of(bar_anchor(monday + timedelta(days=i), exchange),
                              market_tz(exchange))
            for i in range(7)
        }
        assert anchors == {week_anchor(monday, exchange)}

    def test_nse_anchor_is_sunday_1830_utc(self):
        assert week_anchor(date(2026, 7, 15), "NSE") == datetime(
            2026, 7, 12, 18, 30, tzinfo=UTC,
        )


class TestDST:
    """A local day is 23 or 25 hours on a transition day. Offset arithmetic is a
    bug; attaching the zone at the date is not."""

    def test_spring_forward_week_nyse(self):
        before = bar_anchor(date(2026, 3, 6), "NYSE")
        after = bar_anchor(date(2026, 3, 9), "NYSE")
        assert trading_date_of(before, "NYSE") == date(2026, 3, 6)
        assert trading_date_of(after, "NYSE") == date(2026, 3, 9)
        assert after - before != timedelta(days=3)

    def test_fall_back_week_nyse(self):
        before = bar_anchor(date(2026, 10, 30), "NYSE")
        after = bar_anchor(date(2026, 11, 2), "NYSE")
        assert after - before != timedelta(days=3)

    def test_bounds_are_a_local_day_wide_across_dst(self):
        start, end = day_bounds(date(2026, 3, 8), "NYSE")
        assert end - start == timedelta(hours=23)


class TestBounds:

    def test_day_bounds_are_half_open(self):
        d = date(2026, 7, 13)
        start, end = day_bounds(d, "NSE")
        assert start == bar_anchor(d, "NSE")
        assert end == bar_anchor(d + timedelta(days=1), "NSE")

    def test_range_bounds_include_the_last_day(self):
        start, end = range_bounds(date(2026, 7, 13), date(2026, 7, 17), "NSE")
        assert start == bar_anchor(date(2026, 7, 13), "NSE")
        assert end == bar_anchor(date(2026, 7, 18), "NSE")
        assert bar_anchor(date(2026, 7, 17), "NSE") < end


class TestNaiveIsAnError:
    """A naive datetime names no instant. Guessing one is the bug, not the fix."""

    def test_trading_date_of_rejects_naive(self):
        with pytest.raises(ValueError):
            trading_date_of(datetime(2026, 7, 13), "NSE")

    def test_monday_instant_of_rejects_naive(self):
        with pytest.raises(ValueError):
            monday_instant_of(datetime(2026, 7, 13), ZoneInfo("UTC"))


class TestOneImplementation:
    """aggregation._monday_of delegates. If someone re-inlines the arithmetic,
    this catches the copy before it drifts."""

    @pytest.mark.parametrize(
        "tz", ["Asia/Kolkata", "America/New_York", "UTC", "Europe/London"],
    )
    @pytest.mark.parametrize(
        "day", [date(2026, 7, 13), date(2026, 3, 8), date(2026, 11, 1)],
    )
    def test_agrees_with_the_domain(self, tz, day):
        ts = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        assert _monday_of(ts, tz) == monday_instant_of(ts, ZoneInfo(tz))

    def test_monday_of_is_pure_calendar_arithmetic(self):
        for i in range(7):
            assert monday_of(date(2026, 7, 13) + timedelta(days=i)) == date(2026, 7, 13)
