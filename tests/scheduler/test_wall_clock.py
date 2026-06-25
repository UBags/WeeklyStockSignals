"""Tests for the wall-clock tick math.

The functions under test are pure and tz-aware. We pin clock values
deterministically and avoid any sleeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC
from zoneinfo import ZoneInfo

import pytest

from trip_ema.scheduler._wall_clock import (
    seconds_until_next_local_tick,
    seconds_until_next_tick,
    _seconds_until_next_tick,  # backward-compat alias
)


IST = ZoneInfo("Asia/Kolkata")
UTC = UTC


# ---------------------------------------------------------------------------
# seconds_until_next_tick (UTC-epoch anchored, original behaviour)
# ---------------------------------------------------------------------------


class TestSecondsUntilNextTick:
    def test_one_hour_cadence_at_top_of_hour_waits_full_hour(self):
        # 14:00:00 UTC exactly — next tick is 15:00 UTC, one full hour.
        now = datetime(2026, 5, 19, 14, 0, 0, tzinfo=UTC)
        assert seconds_until_next_tick(now, timedelta(hours=1)) == 3600.0

    def test_one_hour_cadence_mid_window(self):
        # 14:17:30 UTC — 42min 30s until 15:00.
        now = datetime(2026, 5, 19, 14, 17, 30, tzinfo=UTC)
        assert seconds_until_next_tick(now, timedelta(hours=1)) == pytest.approx(
            42 * 60 + 30
        )

    def test_15_min_cadence(self):
        # 14:07 UTC — 8min until 14:15.
        now = datetime(2026, 5, 19, 14, 7, 0, tzinfo=UTC)
        assert seconds_until_next_tick(now, timedelta(minutes=15)) == pytest.approx(
            8 * 60
        )

    def test_rejects_naive_datetime(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            seconds_until_next_tick(datetime(2026, 5, 19, 14, 0, 0), timedelta(hours=1))

    def test_rejects_zero_cadence(self):
        with pytest.raises(ValueError, match="positive"):
            seconds_until_next_tick(datetime(2026, 5, 19, tzinfo=UTC), timedelta(0))

    def test_rejects_negative_cadence(self):
        with pytest.raises(ValueError, match="positive"):
            seconds_until_next_tick(
                datetime(2026, 5, 19, tzinfo=UTC), timedelta(seconds=-1)
            )

    def test_tz_aware_non_utc_input_still_works(self):
        # Same instant as 14:00 UTC, expressed in IST (19:30 IST).
        # Should still give 3600s.
        now_ist = datetime(2026, 5, 19, 19, 30, 0, tzinfo=IST)
        assert seconds_until_next_tick(now_ist, timedelta(hours=1)) == 3600.0

    def test_backward_compat_alias(self):
        # The leading-underscore name must remain importable and
        # behave identically.
        now = datetime(2026, 5, 19, 14, 17, 30, tzinfo=UTC)
        assert _seconds_until_next_tick(now, timedelta(hours=1)) == pytest.approx(
            42 * 60 + 30
        )


# ---------------------------------------------------------------------------
# seconds_until_next_local_tick (local-midnight anchored)
# ---------------------------------------------------------------------------


class TestSecondsUntilNextLocalTick:
    def test_ist_top_of_hour_aligns_with_local_clock(self):
        # 09:23:00 IST. Local-midnight anchoring with cadence=1h
        # should yield the next IST top-of-hour: 10:00 IST = 37min away.
        now = datetime(2026, 5, 19, 9, 23, 0, tzinfo=IST)
        result = seconds_until_next_local_tick(now, timedelta(hours=1), IST)
        assert result == pytest.approx(37 * 60)

    def test_ist_at_exact_local_hour_waits_full_cadence(self):
        # 10:00:00 IST exactly. Next tick is 11:00 IST, one full hour.
        now = datetime(2026, 5, 19, 10, 0, 0, tzinfo=IST)
        result = seconds_until_next_local_tick(now, timedelta(hours=1), IST)
        assert result == 3600.0

    def test_input_in_utc_converted_to_local(self):
        # 04:23:00 UTC is 09:53:00 IST. Next local-IST top-of-hour is
        # 10:00 IST = 7min away.
        now_utc = datetime(2026, 5, 19, 4, 23, 0, tzinfo=UTC)
        result = seconds_until_next_local_tick(now_utc, timedelta(hours=1), IST)
        assert result == pytest.approx(7 * 60)

    def test_local_anchoring_differs_from_utc_for_offset_tz(self):
        # IST is UTC+5:30. At 09:00 IST exactly (= 03:30 UTC), the
        # UTC-epoch-anchored fn would say "30 min to top of UTC hour"
        # (04:00 UTC), while the local-anchored fn should say
        # "60 min to next local IST hour" (10:00 IST).
        now = datetime(2026, 5, 19, 9, 0, 0, tzinfo=IST)
        utc_result = seconds_until_next_tick(now, timedelta(hours=1))
        local_result = seconds_until_next_local_tick(now, timedelta(hours=1), IST)
        assert utc_result == pytest.approx(30 * 60)
        assert local_result == pytest.approx(60 * 60)

    def test_quarter_hour_cadence(self):
        # 09:07 IST. Cadence 15min, local-anchored → 09:15 IST = 8min.
        now = datetime(2026, 5, 19, 9, 7, 0, tzinfo=IST)
        result = seconds_until_next_local_tick(now, timedelta(minutes=15), IST)
        assert result == pytest.approx(8 * 60)

    def test_just_before_local_midnight(self):
        # 23:59:30 IST. Next 1-hour tick relative to midnight is
        # 00:00 IST (which is the *next* local midnight) → 30 seconds.
        now = datetime(2026, 5, 19, 23, 59, 30, tzinfo=IST)
        result = seconds_until_next_local_tick(now, timedelta(hours=1), IST)
        assert result == pytest.approx(30)

    def test_rejects_naive(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            seconds_until_next_local_tick(
                datetime(2026, 5, 19, 9, 0, 0), timedelta(hours=1), IST
            )

    def test_rejects_zero_cadence(self):
        with pytest.raises(ValueError, match="positive"):
            seconds_until_next_local_tick(
                datetime(2026, 5, 19, tzinfo=IST), timedelta(0), IST
            )

    def test_dst_transition_us_eastern_spring_forward(self):
        # US Eastern: 2026-03-08 02:00 → 03:00 EDT (skip 02:00-03:00).
        # Set now = 01:30 EST on the transition day. Next 1-hour
        # local tick from local-midnight is 02:00 EST nominally, but
        # 02:00 doesn't exist that day — the offset jumps to EDT.
        # `seconds_until_next_local_tick` returns based on local-clock
        # elapsed since local-midnight, which is 1h30m. Next tick at
        # 2h-elapsed → 30 minutes from now, regardless of DST jump,
        # because we measure elapsed seconds in real time.
        eastern = ZoneInfo("America/New_York")
        now = datetime(2026, 3, 8, 1, 30, 0, tzinfo=eastern)
        result = seconds_until_next_local_tick(now, timedelta(hours=1), eastern)
        # The DST jump means the next *wall-clock* hour boundary is
        # 03:00 EDT = 30 min away in real time. We document this:
        # the function is correct in wall-clock seconds, not in
        # nominal-hour count.
        assert result == pytest.approx(30 * 60)
