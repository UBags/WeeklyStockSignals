"""Tests for the Phase 16a `tick_schedule` resolution path in
`tick_planner.compute_tick_times_for_day` and `next_tick_after`.

What's covered here:

  - Mode A (tick_schedule): NSE four instants on a standard day,
    NYSE four instants on a standard day with rounded midpoint,
    NYSE half-day handling via `close_override`, midpoint rounding
    rule at the minute boundary.
  - Mode B (legacy): bit-for-bit identical to pre-Phase-16a output
    when `tick_schedule=None` — backward compatibility guard.
  - `compute_tick_times_for_day` invariants: UTC tzinfo, ascending,
    deduplicated.
  - `next_tick_after`: walks across days, skips holidays via the
    calendar weekday filter, raises only when lookahead is exhausted.
  - The `_round_to_nearest_minute` helper: half-up at :30s,
    truncation below :30s.

These tests are pure-Python (no DB, no orchestrator threads) and live
in `tests/scheduler_pure/`. The DB-integrated tests in
`tests/scheduler/test_tick_planner.py` continue to validate the legacy
path against the real `TradingCalendar` with python-holidays.
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
    ResolvedTickEntry,
    _round_to_nearest_minute,
    compute_tick_times_for_day,
    next_tick_after,
)


IST = ZoneInfo("Asia/Kolkata")
UTC = UTC
EASTERN = ZoneInfo("America/New_York")


# Canonical Phase 16a schedule. Same shape on every exchange; the
# wall-clock instants differ only because of open/close/tz.
_CANONICAL_SCHEDULE = (
    ResolvedTickEntry(name="post_open",   anchor="open",     offset_minutes=15),
    ResolvedTickEntry(name="mid_session", anchor="midpoint", offset_minutes=0),
    ResolvedTickEntry(name="pre_close",   anchor="close",    offset_minutes=-30),
    ResolvedTickEntry(name="post_close",  anchor="close",    offset_minutes=60),
)


def _nse_with_schedule() -> MarketTickConfig:
    """NSE with the canonical four-tick schedule. Legacy fields are
    set to NSE's old defaults but unused in this mode."""
    return MarketTickConfig(
        exchange="NSE",
        tz=IST,
        open=time(9, 15),
        close=time(15, 30),
        post_open_offset=timedelta(minutes=5),
        pre_close_offset=timedelta(minutes=15),
        intraday_cadence=timedelta(hours=1),
        tick_schedule=_CANONICAL_SCHEDULE,
    )


def _nyse_with_schedule() -> MarketTickConfig:
    return MarketTickConfig(
        exchange="NYSE",
        tz=EASTERN,
        open=time(9, 30),
        close=time(16, 0),
        post_open_offset=timedelta(minutes=5),
        pre_close_offset=timedelta(minutes=15),
        intraday_cadence=timedelta(hours=1),
        tick_schedule=_CANONICAL_SCHEDULE,
    )


def _nse_legacy() -> MarketTickConfig:
    """NSE with NO tick_schedule — should fall through to legacy code path
    and produce the pre-Phase-16a eight-tick output."""
    return MarketTickConfig(
        exchange="NSE_LEGACY",
        tz=IST,
        open=time(9, 15),
        close=time(15, 30),
        post_open_offset=timedelta(minutes=5),
        pre_close_offset=timedelta(minutes=15),
        intraday_cadence=timedelta(hours=1),
        # tick_schedule=None by default
    )


def _calendar_for(exchange: str) -> TradingCalendar:
    """Mon-Fri calendar; no holiday awareness. Matches the pattern in
    `tests/scheduler/test_tick_planner.py`."""
    return TradingCalendar(
        TradingCalendarConfig(
            exchange=exchange,
            weekdays=frozenset([1, 2, 3, 4, 5]),
        )
    )


# ---------------------------------------------------------------------------
# Mode A — NSE standard day
# ---------------------------------------------------------------------------


class TestNseStandardDay:
    """NSE 09:15-15:30 IST → 09:30 / 12:15 / 15:00 / 16:30 IST."""

    def test_four_instants_at_expected_local_times(self):
        # 2026-05-19 is a Tuesday — well clear of NSE holidays.
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nse_with_schedule())
        ticks_ist = [t.astimezone(IST).strftime("%H:%M") for t in ticks]
        assert ticks_ist == ["09:30", "12:15", "15:00", "16:30"]

    def test_count_is_exactly_four(self):
        # Regression guard against future schedule expansion.
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nse_with_schedule())
        assert len(ticks) == 4

    def test_all_ticks_are_utc(self):
        # The planner contract: every returned instant is UTC.
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nse_with_schedule())
        for t in ticks:
            assert t.tzinfo == UTC, f"tick {t} is not UTC"

    def test_ticks_are_strictly_ascending(self):
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nse_with_schedule())
        for prev, nxt in zip(ticks, ticks[1:]):
            assert prev < nxt, f"ticks not ascending: {prev} >= {nxt}"


# ---------------------------------------------------------------------------
# Mode A — NYSE standard day (midpoint rounding bites)
# ---------------------------------------------------------------------------


class TestNyseStandardDay:
    """NYSE 09:30-16:00 ET on a standard session.

    Resolved instants:
      post_open    = 09:30 + 15  = 09:45 ET
      mid_session  = midpoint(09:45, 15:30) = 12:37:30 → rounds to 12:38
      pre_close    = 16:00 - 30  = 15:30 ET
      post_close   = 16:00 + 60  = 17:00 ET
    """

    def test_four_instants_with_midpoint_rounded_up(self):
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nyse_with_schedule())
        ticks_et = [t.astimezone(EASTERN).strftime("%H:%M") for t in ticks]
        assert ticks_et == ["09:45", "12:38", "15:30", "17:00"]

    def test_midpoint_has_zero_seconds_after_rounding(self):
        # Beyond %H:%M presentation — the midpoint must actually
        # land on a whole-minute boundary, not "looks like one in
        # the formatted string but carries seconds underneath".
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nyse_with_schedule())
        midpoint = ticks[1].astimezone(EASTERN)
        assert midpoint.second == 0
        assert midpoint.microsecond == 0


# ---------------------------------------------------------------------------
# Mode A — NYSE half-day via close_override
# ---------------------------------------------------------------------------


class TestNyseHalfDay:
    """NYSE 1pm ET early-close days. With `close_override=time(13, 0)`,
    every close-anchored instant slides — and so does the midpoint
    because it's flanked by the slid pre_close.

    Resolved instants on a 13:00 close:
      post_open    = 09:30 + 15  = 09:45 ET  (unchanged — anchored to open)
      mid_session  = midpoint(09:45, 12:30) = 11:07:30 → rounds to 11:08
      pre_close    = 13:00 - 30  = 12:30 ET
      post_close   = 13:00 + 60  = 14:00 ET
    """

    def test_early_close_shrinks_schedule_proportionally(self):
        ticks = compute_tick_times_for_day(
            date(2026, 5, 19), _nyse_with_schedule(),
            close_override=time(13, 0),
        )
        ticks_et = [t.astimezone(EASTERN).strftime("%H:%M") for t in ticks]
        assert ticks_et == ["09:45", "11:08", "12:30", "14:00"]

    def test_post_open_unaffected_by_close_override(self):
        # The schedule's open-anchored entry should resolve to the same
        # instant whether or not close_override is set — close_override
        # only shifts close-anchored entries (and the midpoint).
        normal = compute_tick_times_for_day(date(2026, 5, 19), _nyse_with_schedule())
        early = compute_tick_times_for_day(
            date(2026, 5, 19), _nyse_with_schedule(),
            close_override=time(13, 0),
        )
        # First instant = post_open in both cases.
        assert normal[0] == early[0]


# ---------------------------------------------------------------------------
# Mode A — explicit midpoint-rounding cases
# ---------------------------------------------------------------------------


class TestMidpointRounding:
    """The midpoint is computed first, then rounded to the nearest minute
    using a deterministic half-up rule. These tests pin the rule in
    place so future "let's use Python's banker's rounding" tweaks
    fail loudly."""

    def test_half_seconds_rounds_up(self):
        # 12:37:30 → 12:38 (half-up, not banker's-rounds-to-even).
        dt = datetime(2026, 5, 19, 12, 37, 30, tzinfo=EASTERN)
        assert _round_to_nearest_minute(dt) == datetime(
            2026, 5, 19, 12, 38, 0, tzinfo=EASTERN,
        )

    def test_one_second_below_half_rounds_down(self):
        # 12:37:29 → 12:37.
        dt = datetime(2026, 5, 19, 12, 37, 29, tzinfo=EASTERN)
        assert _round_to_nearest_minute(dt) == datetime(
            2026, 5, 19, 12, 37, 0, tzinfo=EASTERN,
        )

    def test_one_second_above_half_rounds_up(self):
        # 12:37:31 → 12:38.
        dt = datetime(2026, 5, 19, 12, 37, 31, tzinfo=EASTERN)
        assert _round_to_nearest_minute(dt) == datetime(
            2026, 5, 19, 12, 38, 0, tzinfo=EASTERN,
        )

    def test_whole_minute_passes_through(self):
        # 12:15:00 → 12:15.
        dt = datetime(2026, 5, 19, 12, 15, 0, tzinfo=IST)
        assert _round_to_nearest_minute(dt) == dt

    def test_rounding_across_minute_boundary_when_seconds_equals_half_at_59(self):
        # 12:59:30 → 13:00. Verifies the +1 minute is applied via
        # timedelta, not by setting `minute=60` directly.
        dt = datetime(2026, 5, 19, 12, 59, 30, tzinfo=EASTERN)
        assert _round_to_nearest_minute(dt) == datetime(
            2026, 5, 19, 13, 0, 0, tzinfo=EASTERN,
        )

    def test_rounding_across_hour_and_day_boundary(self):
        # 23:59:45 → 00:00 next day.
        dt = datetime(2026, 5, 19, 23, 59, 45, tzinfo=EASTERN)
        assert _round_to_nearest_minute(dt) == datetime(
            2026, 5, 20, 0, 0, 0, tzinfo=EASTERN,
        )


# ---------------------------------------------------------------------------
# Mode B — backward compatibility (no tick_schedule on the config)
# ---------------------------------------------------------------------------


class TestLegacyPathBackCompat:
    """A MarketTickConfig with tick_schedule=None must produce the
    bit-for-bit same schedule as the pre-Phase-16a code did. This is
    the regression guard for every operator who hasn't migrated their
    config.yaml yet."""

    def test_nse_eight_tick_legacy_schedule_unchanged(self):
        ticks = compute_tick_times_for_day(date(2026, 5, 19), _nse_legacy())
        ticks_ist = [t.astimezone(IST).strftime("%H:%M") for t in ticks]
        assert ticks_ist == [
            "09:20", "10:00", "11:00", "12:00",
            "13:00", "14:00", "15:00", "15:15",
        ]
        assert len(ticks) == 8

    def test_close_override_with_legacy_path_emits_warning_and_ignores(self, caplog):
        # close_override is a Mode-A concept. Passing it in legacy mode
        # should not silently change the output; it should warn AND
        # produce the unchanged legacy schedule.
        with caplog.at_level("WARNING"):
            ticks_with_override = compute_tick_times_for_day(
                date(2026, 5, 19), _nse_legacy(),
                close_override=time(13, 0),
            )
        ticks_baseline = compute_tick_times_for_day(date(2026, 5, 19), _nse_legacy())
        assert ticks_with_override == ticks_baseline
        # The warning text mentions the market and the value, so it's
        # actionable in /tmp/trip-ema.log.
        assert any(
            "NSE_LEGACY" in rec.message and "close_override" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# next_tick_after with the new schedule
# ---------------------------------------------------------------------------


class TestNextTickAfterWithSchedule:
    """The walker function must work the same way regardless of which
    code path computes the per-day ticks. These tests use the Phase
    16a path; the legacy-path equivalents are in
    `tests/scheduler/test_tick_planner.py`."""

    def test_returns_first_tick_after_now_same_day(self):
        # 2026-05-19 Tuesday 08:00 IST (one hour before NSE post_open).
        # First tick today = 09:30 IST = 04:00 UTC.
        now = datetime(2026, 5, 19, 2, 30, 0, tzinfo=UTC)  # = 08:00 IST
        nxt = next_tick_after(now, _nse_with_schedule(), _calendar_for("NSE"))
        assert nxt.astimezone(IST) == datetime(2026, 5, 19, 9, 30, tzinfo=IST)

    def test_skips_to_next_tick_when_between_two_ticks_today(self):
        # 10:00 IST → next is 12:15 IST.
        now = datetime(2026, 5, 19, 10, 0, tzinfo=IST).astimezone(UTC)
        nxt = next_tick_after(now, _nse_with_schedule(), _calendar_for("NSE"))
        assert nxt.astimezone(IST) == datetime(2026, 5, 19, 12, 15, tzinfo=IST)

    def test_walks_to_next_day_when_after_last_tick_today(self):
        # Tuesday 17:00 IST is after today's post_close (16:30).
        # Next trading day = Wednesday; first tick = Wed 09:30 IST.
        now = datetime(2026, 5, 19, 17, 0, tzinfo=IST).astimezone(UTC)
        nxt = next_tick_after(now, _nse_with_schedule(), _calendar_for("NSE"))
        assert nxt.astimezone(IST) == datetime(2026, 5, 20, 9, 30, tzinfo=IST)

    def test_walks_across_weekend(self):
        # Friday 17:00 IST → Monday 09:30 IST.
        # 2026-05-22 is Friday; 2026-05-25 is Monday.
        now = datetime(2026, 5, 22, 17, 0, tzinfo=IST).astimezone(UTC)
        nxt = next_tick_after(now, _nse_with_schedule(), _calendar_for("NSE"))
        assert nxt.astimezone(IST) == datetime(2026, 5, 25, 9, 30, tzinfo=IST)

    def test_now_must_be_timezone_aware(self):
        naive = datetime(2026, 5, 19, 10, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            next_tick_after(naive, _nse_with_schedule(), _calendar_for("NSE"))

    def test_raises_when_no_trading_days_in_lookahead(self):
        # Calendar that has NO weekdays open at all.
        cal = TradingCalendar(
            TradingCalendarConfig(
                exchange="NEVER",
                weekdays=frozenset(),
            )
        )
        now = datetime(2026, 5, 19, 10, 0, tzinfo=UTC)
        with pytest.raises(RuntimeError, match="No tick found"):
            next_tick_after(now, _nse_with_schedule(), cal, max_lookahead_days=3)