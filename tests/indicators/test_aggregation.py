"""Tests for daily-to-weekly aggregation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.domain import OHLCVBar, Timeframe
from trip_ema.indicators.aggregation import (
    _monday_of,
    bucket_end,
    daily_to_weekly,
    is_provisional,
)


def _d(year: int, month: int, day: int, o: float, h: float, l: float, c: float, v: float) -> OHLCVBar:
    return OHLCVBar(
        timestamp=datetime(year, month, day, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c, volume=v,
    )


def test_empty_input_returns_empty():
    assert daily_to_weekly([]) == []


def test_single_day_returns_single_weekly_bar_when_partial_allowed():
    # Tuesday alone — partial week, but include_partial_last_week defaults True
    bars = [_d(2024, 1, 2, 100, 110, 95, 105, 1000)]
    weekly = daily_to_weekly(bars)
    assert len(weekly) == 1
    assert weekly[0].open == 100
    assert weekly[0].close == 105
    assert weekly[0].high == 110
    assert weekly[0].low == 95
    assert weekly[0].volume == 1000


def test_partial_week_excluded_when_requested():
    bars = [_d(2024, 1, 2, 100, 110, 95, 105, 1000)]
    weekly = daily_to_weekly(bars, include_partial_last_week=False)
    assert weekly == []


def test_full_week_aggregated_correctly():
    # Mon 2024-01-01 through Fri 2024-01-05
    bars = [
        _d(2024, 1, 1, 100, 105, 99, 103, 1000),
        _d(2024, 1, 2, 103, 108, 102, 107, 1100),
        _d(2024, 1, 3, 107, 112, 106, 110, 1200),
        _d(2024, 1, 4, 110, 115, 109, 114, 1300),
        _d(2024, 1, 5, 114, 118, 113, 116, 1400),
    ]
    weekly = daily_to_weekly(bars, include_partial_last_week=False)
    assert len(weekly) == 1
    w = weekly[0]
    assert w.open == 100       # Monday's open
    assert w.close == 116      # Friday's close
    assert w.high == 118       # max of Friday
    assert w.low == 99         # min of Monday
    assert w.volume == 6000    # sum
    # Timestamp is Monday midnight UTC
    assert w.timestamp == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_multiple_weeks_split_correctly():
    # Week 1: Mon Jan 1 - Fri Jan 5
    # Week 2: Mon Jan 8 - Tue Jan 9 (partial)
    bars = [
        _d(2024, 1, 1, 100, 105, 99, 103, 1000),
        _d(2024, 1, 5, 110, 115, 105, 112, 1500),
        _d(2024, 1, 8, 113, 120, 112, 118, 2000),
        _d(2024, 1, 9, 118, 122, 117, 121, 2100),
    ]
    weekly = daily_to_weekly(bars)
    assert len(weekly) == 2
    assert weekly[0].open == 100
    assert weekly[0].close == 112
    assert weekly[0].timestamp == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert weekly[1].open == 113
    assert weekly[1].close == 121
    assert weekly[1].volume == 4100
    assert weekly[1].timestamp == datetime(2024, 1, 8, tzinfo=timezone.utc)


def test_mid_week_start_handled():
    """If history begins mid-week (e.g. on Wednesday), the first weekly bar's
    open is Wednesday's open and only the days seen are aggregated."""
    bars = [
        _d(2024, 1, 3, 105, 110, 104, 108, 1000),   # Wednesday
        _d(2024, 1, 4, 108, 113, 107, 111, 1100),   # Thursday
        _d(2024, 1, 5, 111, 115, 110, 114, 1200),   # Friday
    ]
    weekly = daily_to_weekly(bars)
    assert len(weekly) == 1
    w = weekly[0]
    assert w.open == 105     # Wednesday's open
    assert w.close == 114    # Friday's close
    # Week timestamp is still the Monday of that week (Jan 1)
    assert w.timestamp == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_weekend_bars_grouped_with_preceding_week():
    """Some exchanges (crypto) trade on weekends. Sat/Sun fall into the
    same ISO week as the preceding Monday. Verify our implementation
    handles this — Saturday's weekday() is 5, Sunday's is 6."""
    bars = [
        _d(2024, 1, 5, 110, 115, 105, 112, 1500),   # Friday
        _d(2024, 1, 6, 112, 118, 111, 117, 800),    # Saturday
        _d(2024, 1, 7, 117, 120, 115, 119, 700),    # Sunday
    ]
    weekly = daily_to_weekly(bars)
    assert len(weekly) == 1
    assert weekly[0].timestamp == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert weekly[0].close == 119   # Sunday's close
    assert weekly[0].volume == 3000


# ============================================================
# B4: Timezone-aware aggregation
# ============================================================

from zoneinfo import ZoneInfo


class TestTimezoneAwareMondayOf:
    """The week-anchor for different exchanges lands at different UTC
    instants. Verify _monday_of honors the supplied timezone."""

    def test_utc_monday_unchanged(self):
        # Sunday 2026-05-17 12:00 UTC → Monday 2026-05-11 in UTC week
        # (a Sunday is the *last* day of the prior ISO week)
        sun = datetime(2026, 5, 17, 12, tzinfo=timezone.utc)
        assert _monday_of(sun, tz="UTC") == datetime(
            2026, 5, 11, tzinfo=timezone.utc
        )

    def test_ist_monday_is_530_earlier_in_utc(self):
        """IST Monday 00:00 = previous Sunday 18:30 UTC.

        For Mon 2026-05-18 in IST, the week boundary is Sun 2026-05-17
        18:30 UTC. Anything before that, in IST, was still last week."""
        # A timestamp that's Sunday 19:00 UTC, which is Monday 00:30 IST —
        # in IST terms this is already in the new week.
        sun_late_utc = datetime(2026, 5, 17, 19, tzinfo=timezone.utc)
        expected_monday_utc = datetime(2026, 5, 17, 18, 30, tzinfo=timezone.utc)
        assert _monday_of(sun_late_utc, tz="Asia/Kolkata") == expected_monday_utc

    def test_ist_monday_for_timestamp_just_before_boundary(self):
        """Sunday 18:00 UTC = Sunday 23:30 IST — still last week in IST."""
        sun_before = datetime(2026, 5, 17, 18, tzinfo=timezone.utc)
        # Previous Monday in IST = Mon 2026-05-11 IST = Sun 2026-05-10 18:30 UTC
        expected = datetime(2026, 5, 10, 18, 30, tzinfo=timezone.utc)
        assert _monday_of(sun_before, tz="Asia/Kolkata") == expected

    def test_et_monday_during_dst(self):
        """In DST (EDT, UTC-4), Monday 00:00 ET = Monday 04:00 UTC.
        May 2026 is well inside DST (DST is March → November)."""
        wed = datetime(2026, 5, 20, 16, tzinfo=timezone.utc)
        # Monday 2026-05-18 in ET → 04:00 UTC
        expected = datetime(2026, 5, 18, 4, tzinfo=timezone.utc)
        assert _monday_of(wed, tz="America/New_York") == expected

    def test_et_monday_during_standard_time(self):
        """In EST (UTC-5), Monday 00:00 ET = Monday 05:00 UTC."""
        # December — standard time
        dec = datetime(2026, 12, 16, 16, tzinfo=timezone.utc)
        # Monday 2026-12-14 in ET → 05:00 UTC
        expected = datetime(2026, 12, 14, 5, tzinfo=timezone.utc)
        assert _monday_of(dec, tz="America/New_York") == expected

    def test_naive_datetime_rejected(self):
        naive = datetime(2026, 5, 20, 16)
        with pytest.raises(ValueError, match="tz-aware"):
            _monday_of(naive, tz="UTC")


class TestTimezoneAwareBucketEnd:
    """bucket_end and is_provisional must honor the tz argument."""

    def test_weekly_bucket_in_utc(self):
        mon = datetime(2026, 5, 18, tzinfo=timezone.utc)
        end = bucket_end(mon, Timeframe.WEEKLY, tz="UTC")
        assert end == datetime(2026, 5, 25, tzinfo=timezone.utc)

    def test_weekly_bucket_in_ist(self):
        """The IST-Monday bar's bucket ends at the next IST-Monday."""
        # IST Monday 2026-05-18 = Sun 2026-05-17 18:30 UTC
        mon_ist = datetime(2026, 5, 17, 18, 30, tzinfo=timezone.utc)
        end = bucket_end(mon_ist, Timeframe.WEEKLY, tz="Asia/Kolkata")
        # Next IST Monday = 2026-05-25 IST = 2026-05-24 18:30 UTC
        assert end == datetime(2026, 5, 24, 18, 30, tzinfo=timezone.utc)

    def test_is_provisional_during_ist_week(self):
        """Tuesday afternoon IST: a Monday-IST weekly bar is still
        provisional in the IST sense."""
        # IST Mon = Sun 18:30 UTC; we're at Tue 14:00 UTC = Tue 19:30 IST
        mon_ist_utc = datetime(2026, 5, 17, 18, 30, tzinfo=timezone.utc)
        tue_afternoon_utc = datetime(2026, 5, 19, 14, tzinfo=timezone.utc)
        assert is_provisional(
            mon_ist_utc, Timeframe.WEEKLY, tue_afternoon_utc, tz="Asia/Kolkata"
        )

    def test_is_provisional_after_ist_week_close(self):
        """Sunday IST afternoon: the prior IST-Monday weekly bar's
        bucket has closed (next IST-Monday starts at Sun 18:30 UTC)."""
        mon_ist_utc = datetime(2026, 5, 17, 18, 30, tzinfo=timezone.utc)
        # Sunday 2026-05-24 20:00 UTC = Mon 2026-05-25 01:30 IST — new week
        next_sun_late = datetime(2026, 5, 24, 20, tzinfo=timezone.utc)
        assert not is_provisional(
            mon_ist_utc, Timeframe.WEEKLY, next_sun_late, tz="Asia/Kolkata"
        )


class TestDailyToWeeklyTimezoneAware:
    """daily_to_weekly should bucket each bar by the supplied tz."""

    def test_daily_bars_aggregate_into_one_ist_week(self):
        """5 daily bars Mon-Fri (all in IST) should aggregate to one
        weekly bar anchored at IST Monday."""
        # IST Monday 2026-05-18 00:00 = 2026-05-17 18:30 UTC
        ist_mon_utc = datetime(2026, 5, 17, 18, 30, tzinfo=timezone.utc)
        bars = [
            OHLCVBar(
                timestamp=ist_mon_utc + timedelta(days=i),
                open=100 + i,
                high=102 + i,
                low=99 + i,
                close=101 + i,
                volume=1000.0,
            )
            for i in range(5)   # Mon-Fri IST
        ]
        weekly = daily_to_weekly(bars, tz="Asia/Kolkata")
        assert len(weekly) == 1
        assert weekly[0].timestamp == ist_mon_utc
        assert weekly[0].open == 100
        assert weekly[0].close == 105   # Fri close
        assert weekly[0].high == 106    # Fri high
        assert weekly[0].volume == 5000

    def test_same_bars_in_utc_vs_ist_can_anchor_differently(self):
        """A bar at Sunday 19:00 UTC is in different ISO weeks in UTC
        vs IST (in UTC, it's still Sunday of the prior week; in IST,
        it's Monday 00:30 of the new week)."""
        sun_late_utc = datetime(2026, 5, 17, 19, tzinfo=timezone.utc)
        bar = OHLCVBar(
            timestamp=sun_late_utc, open=100, high=101, low=99,
            close=100.5, volume=500.0,
        )
        utc_weekly = daily_to_weekly([bar], tz="UTC")
        ist_weekly = daily_to_weekly([bar], tz="Asia/Kolkata")
        # The weekly bars exist in both views but anchor at different Mondays
        assert utc_weekly[0].timestamp != ist_weekly[0].timestamp
        # UTC: Mon 2026-05-11 (still in the prior week per UTC ISO)
        assert utc_weekly[0].timestamp == datetime(2026, 5, 11, tzinfo=timezone.utc)
        # IST: Mon 2026-05-18 IST = Sun 2026-05-17 18:30 UTC
        assert ist_weekly[0].timestamp == datetime(2026, 5, 17, 18, 30, tzinfo=timezone.utc)


class TestDailyToWeeklyWithFinerSource:
    """daily_to_weekly handles any finer-than-weekly source — it just
    buckets by week regardless of how many input bars per week."""

    def test_hourly_bars_aggregate_to_weekly(self):
        """24 hourly bars across one day → 1 weekly bar (partial week)."""
        mon = datetime(2026, 5, 18, tzinfo=timezone.utc)
        bars = [
            OHLCVBar(
                timestamp=mon + timedelta(hours=i),
                open=100 + i * 0.1, high=100.5 + i * 0.1,
                low=99.5 + i * 0.1, close=100.2 + i * 0.1,
                volume=10.0,
            )
            for i in range(24)
        ]
        weekly = daily_to_weekly(bars, tz="UTC", include_partial_last_week=True)
        assert len(weekly) == 1
        # Open = first hour's open, close = last hour's close
        assert weekly[0].open == pytest.approx(100.0)
        assert weekly[0].close == pytest.approx(100.2 + 23 * 0.1)
        assert weekly[0].volume == 240.0


# ============================================================
# Phase 9 — B1 regression: DST drift in bucket_end
#
# The buggy implementation added timedelta(days=7) (= 168h of UTC-elapsed)
# to a UTC-converted Monday. On DST transition weeks in America/New_York,
# Europe/London, etc., the local week is 167h or 169h, so the result was
# off by an hour for ~2 weeks per year. The fix does calendar arithmetic
# on the local date instead.
# ============================================================


class TestBucketEndDST:
    """bucket_end and is_provisional must return correct boundaries
    across DST transitions in zones that observe DST."""

    # America/New_York DST transitions (well-known dates):
    #   Spring forward: 2nd Sunday of March, 02:00 EST → 03:00 EDT
    #   Fall back:      1st Sunday of November, 02:00 EDT → 01:00 EST
    # In 2026:
    #   Spring forward: Sunday 2026-03-08, 02:00 EST → 03:00 EDT
    #   Fall back:      Sunday 2026-11-01, 02:00 EDT → 01:00 EST

    # ---------- WEEKLY ----------

    def test_weekly_bucket_in_ny_during_spring_forward_week(self):
        """The week of 2026-03-02 (Mon) → 2026-03-09 (Mon) in NY contains
        the spring-forward transition. Local week is 167h. The bucket
        must end at exactly midnight local-time of Mon 2026-03-09 EDT,
        which is 04:00 UTC (NOT 05:00 UTC, which is what the buggy
        implementation would produce by adding 168h to 05:00 UTC)."""
        # Monday 2026-03-02 00:00 EST = 2026-03-02 05:00 UTC
        monday_2026_03_02 = datetime(2026, 3, 2, 5, 0, tzinfo=timezone.utc)
        end = bucket_end(monday_2026_03_02, Timeframe.WEEKLY, tz="America/New_York")
        # Monday 2026-03-09 00:00 EDT = 2026-03-09 04:00 UTC (after DST)
        expected = datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc)
        assert end == expected, (
            f"Expected bucket end at {expected} (next Monday midnight EDT, "
            f"04:00 UTC after spring-forward), got {end}. "
            f"If got {datetime(2026, 3, 9, 5, 0, tzinfo=timezone.utc)}, "
            f"DST drift bug has regressed."
        )

    def test_weekly_bucket_in_ny_during_fall_back_week(self):
        """The week of 2026-10-26 (Mon) → 2026-11-02 (Mon) in NY contains
        the fall-back transition. Local week is 169h. The bucket must
        end at exactly midnight local-time of Mon 2026-11-02 EST,
        which is 05:00 UTC (NOT 04:00 UTC, which is what the buggy
        implementation would produce)."""
        # Monday 2026-10-26 00:00 EDT = 2026-10-26 04:00 UTC
        monday_2026_10_26 = datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)
        end = bucket_end(monday_2026_10_26, Timeframe.WEEKLY, tz="America/New_York")
        # Monday 2026-11-02 00:00 EST = 2026-11-02 05:00 UTC (after fall-back)
        expected = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
        assert end == expected, (
            f"Expected bucket end at {expected} (next Monday midnight EST, "
            f"05:00 UTC after fall-back), got {end}. "
            f"If got {datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)}, "
            f"DST drift bug has regressed."
        )

    def test_weekly_bucket_in_ny_non_dst_week_unchanged(self):
        """Sanity: regular non-DST weeks must give the same answer as
        timedelta(days=7) would have. The fix must not break the
        common case."""
        # Monday 2026-06-01 00:00 EDT = 2026-06-01 04:00 UTC
        monday = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)
        end = bucket_end(monday, Timeframe.WEEKLY, tz="America/New_York")
        expected = datetime(2026, 6, 8, 4, 0, tzinfo=timezone.utc)
        assert end == expected

    def test_weekly_bucket_in_utc_unaffected(self):
        """UTC zone doesn't observe DST — fix must be a no-op here."""
        monday = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
        end = bucket_end(monday, Timeframe.WEEKLY, tz="UTC")
        assert end == datetime(2026, 3, 9, 0, 0, tzinfo=timezone.utc)

    def test_weekly_bucket_in_ist_unaffected(self):
        """IST (Asia/Kolkata) doesn't observe DST — fix must be a no-op."""
        # Monday 2026-03-02 00:00 IST = 2026-03-01 18:30 UTC
        monday = datetime(2026, 3, 1, 18, 30, tzinfo=timezone.utc)
        end = bucket_end(monday, Timeframe.WEEKLY, tz="Asia/Kolkata")
        # Next IST Monday: 2026-03-09 00:00 IST = 2026-03-08 18:30 UTC
        expected = datetime(2026, 3, 8, 18, 30, tzinfo=timezone.utc)
        assert end == expected

    # ---------- DAILY ----------

    def test_daily_bucket_in_ny_on_spring_forward_day(self):
        """Sunday 2026-03-08 in NY is the spring-forward day (23h local).
        The daily bucket for Sunday must end at midnight EDT on Monday
        2026-03-09, i.e., 04:00 UTC."""
        # Sunday 2026-03-08 00:00 EST = 2026-03-08 05:00 UTC
        sunday = datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)
        end = bucket_end(sunday, Timeframe.DAILY, tz="America/New_York")
        # Monday 2026-03-09 00:00 EDT = 2026-03-09 04:00 UTC
        expected = datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc)
        assert end == expected

    def test_daily_bucket_in_ny_on_fall_back_day(self):
        """Sunday 2026-11-01 in NY is the fall-back day (25h local).
        The daily bucket must end at midnight EST on Monday 2026-11-02,
        i.e., 05:00 UTC."""
        # Sunday 2026-11-01 00:00 EDT = 2026-11-01 04:00 UTC
        sunday = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
        end = bucket_end(sunday, Timeframe.DAILY, tz="America/New_York")
        # Monday 2026-11-02 00:00 EST = 2026-11-02 05:00 UTC
        expected = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
        assert end == expected

    # ---------- is_provisional propagation ----------

    def test_is_provisional_correct_across_spring_forward(self):
        """The 1-hour window between 03:00 UTC and 04:00 UTC on Monday
        2026-03-09 should be POST-bucket-end (bucket ended at 04:00 UTC
        after spring-forward). The buggy implementation would have said
        the bucket ended at 05:00 UTC, marking 03:00-05:00 UTC of Monday
        as still provisional for the prior week, which is wrong by 1h."""
        monday_2026_03_02 = datetime(2026, 3, 2, 5, 0, tzinfo=timezone.utc)
        # 03:30 UTC Mon 2026-03-09 is local 23:30 EDT Sun — that's still
        # technically Sunday evening in NY, BUT the bucket end is at
        # 04:00 UTC, so this IS within the bucket.
        within = datetime(2026, 3, 9, 3, 30, tzinfo=timezone.utc)
        assert is_provisional(monday_2026_03_02, Timeframe.WEEKLY, within, tz="America/New_York")
        # 04:30 UTC Mon 2026-03-09 is local 00:30 EDT Mon — bucket has ended.
        beyond = datetime(2026, 3, 9, 4, 30, tzinfo=timezone.utc)
        assert not is_provisional(monday_2026_03_02, Timeframe.WEEKLY, beyond, tz="America/New_York")


# ============================================================
# Phase 9 — A2 gap detection helpers
# ============================================================


class TestExpectedClosedWeeklyBuckets:
    """expected_closed_weekly_buckets enumerates the closed weeks
    between engine_last_ts and now that the ETL should have data for."""

    def _utc(self, y, mo, d):
        return datetime(y, mo, d, tzinfo=timezone.utc)

    def test_same_week_returns_empty(self):
        """If engine_last_ts and now are in the same local week, there
        are no closed buckets between them."""
        from trip_ema.indicators.aggregation import expected_closed_weekly_buckets
        engine_last = self._utc(2026, 5, 11)  # Monday
        now = self._utc(2026, 5, 14)           # Thursday same week
        result = expected_closed_weekly_buckets(engine_last, now, tz="UTC")
        assert result == []

    def test_one_week_gap(self):
        """engine_last in week W, now in week W+2: one closed bucket (W+1)."""
        from trip_ema.indicators.aggregation import expected_closed_weekly_buckets
        engine_last = self._utc(2026, 5, 4)   # Monday 5/4
        now = self._utc(2026, 5, 19)           # Tuesday 5/19 (week of 5/18)
        result = expected_closed_weekly_buckets(engine_last, now, tz="UTC")
        assert result == [self._utc(2026, 5, 11)]

    def test_three_week_gap(self):
        from trip_ema.indicators.aggregation import expected_closed_weekly_buckets
        engine_last = self._utc(2026, 5, 4)   # Monday
        now = self._utc(2026, 6, 2)            # Tuesday of week 6/1
        result = expected_closed_weekly_buckets(engine_last, now, tz="UTC")
        assert result == [
            self._utc(2026, 5, 11),
            self._utc(2026, 5, 18),
            self._utc(2026, 5, 25),
        ]

    def test_consecutive_weeks_no_gap(self):
        """engine_last in week W, now in week W+1: zero closed buckets
        (the current week doesn't count as closed)."""
        from trip_ema.indicators.aggregation import expected_closed_weekly_buckets
        engine_last = self._utc(2026, 5, 4)
        now = self._utc(2026, 5, 13)           # Wednesday of week 5/11
        result = expected_closed_weekly_buckets(engine_last, now, tz="UTC")
        assert result == []

    def test_naive_datetime_rejected(self):
        from trip_ema.indicators.aggregation import expected_closed_weekly_buckets
        with pytest.raises(ValueError):
            expected_closed_weekly_buckets(
                datetime(2026, 5, 1), datetime(2026, 5, 20), tz="UTC",
            )

    def test_ist_anchor_one_week_gap(self):
        """Verify expected buckets use IST week-anchor (Mon 00:00 IST =
        Sun 18:30 UTC)."""
        from trip_ema.indicators.aggregation import expected_closed_weekly_buckets
        # IST Monday 2026-05-04 00:00 = 2026-05-03 18:30 UTC
        engine_last = datetime(2026, 5, 3, 18, 30, tzinfo=timezone.utc)
        now = self._utc(2026, 5, 19)
        result = expected_closed_weekly_buckets(engine_last, now, tz="Asia/Kolkata")
        # Expected: IST Mon 5/11 (= UTC 2026-05-10 18:30)
        assert result == [datetime(2026, 5, 10, 18, 30, tzinfo=timezone.utc)]


class TestDetectWeeklyGaps:
    """detect_weekly_gaps checks for missing source bars in expected
    closed weeks."""

    def _utc(self, y, mo, d):
        return datetime(y, mo, d, tzinfo=timezone.utc)

    def test_no_gap_when_all_expected_weeks_have_bars(self):
        from trip_ema.indicators.aggregation import detect_weekly_gaps
        engine_last = self._utc(2026, 5, 4)
        now = self._utc(2026, 5, 26)
        # Expected closed weeks: 5/11 and 5/18.
        # Provide at least one daily bar per expected week.
        source_ts = [
            self._utc(2026, 5, 12),   # week of 5/11
            self._utc(2026, 5, 19),   # week of 5/18
            self._utc(2026, 5, 25),   # current week (partial)
        ]
        assert detect_weekly_gaps(source_ts, engine_last, now, tz="UTC") == []

    def test_gap_when_a_week_has_no_bars(self):
        from trip_ema.indicators.aggregation import detect_weekly_gaps
        engine_last = self._utc(2026, 5, 4)
        now = self._utc(2026, 5, 26)
        # Expected closed weeks: 5/11 and 5/18. Provide only 5/11.
        source_ts = [
            self._utc(2026, 5, 12),
            self._utc(2026, 5, 25),   # current week
        ]
        gaps = detect_weekly_gaps(source_ts, engine_last, now, tz="UTC")
        assert gaps == [self._utc(2026, 5, 18)]

    def test_current_week_being_empty_is_not_a_gap(self):
        from trip_ema.indicators.aggregation import detect_weekly_gaps
        engine_last = self._utc(2026, 5, 4)
        now = self._utc(2026, 5, 12)  # Tuesday of next week — no bars yet
        # Expected closed weeks: none (engine_last and now's prior week
        # are adjacent — only the current week sits between).
        assert detect_weekly_gaps([], engine_last, now, tz="UTC") == []

    def test_no_source_bars_with_expected_buckets_means_gap(self):
        """If multiple closed weeks are expected and zero source bars
        exist, every expected bucket is a gap."""
        from trip_ema.indicators.aggregation import detect_weekly_gaps
        engine_last = self._utc(2026, 5, 4)
        now = self._utc(2026, 6, 2)
        gaps = detect_weekly_gaps([], engine_last, now, tz="UTC")
        assert gaps == [
            self._utc(2026, 5, 11),
            self._utc(2026, 5, 18),
            self._utc(2026, 5, 25),
        ]


# ============================================================
# include_partial_last_week threshold rework
#
# Pre-rework: hardcoded "≥ 5 bars = complete week" — correct for daily
# source, wrong for hourly (where a week is ~32 bars) or 15m (~130).
# Rework: min_bars_for_complete_week parameter (default 5 = back-compat),
# plus a recommended_min_bars_per_week(source_tf) helper.
# ============================================================


class TestMinBarsForCompleteWeek:

    UTC = timezone.utc

    def test_recommended_thresholds_per_source_timeframe(self):
        from trip_ema.indicators.aggregation import recommended_min_bars_per_week
        # Calibration assumes ~32 hourly bars / week on equities
        assert recommended_min_bars_per_week(Timeframe.DAILY) == 5
        assert recommended_min_bars_per_week(Timeframe.HOURLY) == 20
        assert recommended_min_bars_per_week(Timeframe.HOURLY_2) == 10
        assert recommended_min_bars_per_week(Timeframe.MIN_15) == 80
        assert recommended_min_bars_per_week(Timeframe.WEEKLY) == 1

    def test_hourly_partial_week_not_emitted_with_correct_threshold(self):
        """With hourly source, 10 bars on Monday is NOT a complete week;
        threshold=20 must keep it out when include_partial_last_week=False."""
        from trip_ema.indicators.aggregation import daily_to_weekly
        start = datetime(2026, 5, 4, tzinfo=self.UTC)  # Monday
        bars = [
            OHLCVBar(
                timestamp=start + timedelta(hours=i),
                open=100.0, high=101.0, low=99.0, close=100.0, volume=10.0,
            )
            for i in range(10)
        ]
        weekly = daily_to_weekly(
            bars, include_partial_last_week=False, tz="UTC",
            min_bars_for_complete_week=20,
        )
        assert weekly == []

    def test_hourly_complete_week_emitted_above_threshold(self):
        from trip_ema.indicators.aggregation import daily_to_weekly
        start = datetime(2026, 5, 4, tzinfo=self.UTC)
        bars = [
            OHLCVBar(
                timestamp=start + timedelta(hours=i),
                open=100.0, high=101.0, low=99.0, close=100.0, volume=10.0,
            )
            for i in range(25)  # well above threshold=20
        ]
        weekly = daily_to_weekly(
            bars, include_partial_last_week=False, tz="UTC",
            min_bars_for_complete_week=20,
        )
        assert len(weekly) == 1

    def test_default_threshold_back_compat(self):
        """Without explicit min_bars_for_complete_week, the legacy
        threshold of 5 must remain in effect."""
        from trip_ema.indicators.aggregation import daily_to_weekly
        start = datetime(2026, 5, 4, tzinfo=self.UTC)
        # 5 daily bars — meets the legacy threshold exactly
        bars = [
            OHLCVBar(
                timestamp=start + timedelta(days=i),
                open=100.0, high=101.0, low=99.0, close=100.0, volume=10.0,
            )
            for i in range(5)
        ]
        weekly = daily_to_weekly(bars, include_partial_last_week=False, tz="UTC")
        assert len(weekly) == 1  # legacy ≥5 says complete

        # 4 daily bars — below legacy threshold, must be excluded
        weekly_4 = daily_to_weekly(
            bars[:4], include_partial_last_week=False, tz="UTC",
        )
        assert weekly_4 == []

    def test_include_partial_true_bypasses_threshold(self):
        """When include_partial_last_week=True, the threshold doesn't
        matter — even a single bar makes a (partial) week."""
        from trip_ema.indicators.aggregation import daily_to_weekly
        start = datetime(2026, 5, 4, tzinfo=self.UTC)
        bars = [
            OHLCVBar(
                timestamp=start,
                open=100.0, high=101.0, low=99.0, close=100.0, volume=10.0,
            )
        ]
        weekly = daily_to_weekly(
            bars, include_partial_last_week=True, tz="UTC",
            min_bars_for_complete_week=100,  # irrelevant
        )
        assert len(weekly) == 1

