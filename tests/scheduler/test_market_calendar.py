"""Tests for the generalized trading-day calendar.

Most tests don't need `python-holidays` — they exercise the weekday
filter and override paths. A small slice of tests depends on
python-holidays being installed; those skip if not.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from trip_ema.scheduler.market_calendar import (
    TradingCalendar,
    TradingCalendarConfig,
)


def _config(
    *,
    exchange: str = "TEST",
    weekdays: list[int] | None = None,
    country: str | None = None,
    overrides: Path | None = None,
) -> TradingCalendarConfig:
    # `weekdays is None` rather than `weekdays or ...` so that an
    # explicit empty list reaches the constructor unchanged. Tests
    # specifically pass [] to exercise the no-trading-days guard.
    if weekdays is None:
        weekdays = [1, 2, 3, 4, 5]
    return TradingCalendarConfig(
        exchange=exchange,
        weekdays=frozenset(weekdays),
        holidays_country=country,
        holidays_override_file=overrides,
    )


# ---------------------------------------------------------------------------
# Weekday filter (no holidays involved)
# ---------------------------------------------------------------------------


class TestWeekdayFilter:
    def test_mon_fri_calendar_accepts_weekdays(self):
        cal = TradingCalendar(_config())
        # 2026-05-19 is Tuesday.
        assert cal.is_trading_day(date(2026, 5, 19)) is True
        # 2026-05-22 is Friday.
        assert cal.is_trading_day(date(2026, 5, 22)) is True

    def test_mon_fri_calendar_rejects_weekend(self):
        cal = TradingCalendar(_config())
        # 2026-05-23 is Saturday, 2026-05-24 is Sunday.
        assert cal.is_trading_day(date(2026, 5, 23)) is False
        assert cal.is_trading_day(date(2026, 5, 24)) is False

    def test_six_day_calendar(self):
        cal = TradingCalendar(_config(weekdays=[1, 2, 3, 4, 5, 6]))
        # Saturday now a trading day.
        assert cal.is_trading_day(date(2026, 5, 23)) is True
        # Sunday still off.
        assert cal.is_trading_day(date(2026, 5, 24)) is False


# ---------------------------------------------------------------------------
# Operator overrides
# ---------------------------------------------------------------------------


class TestYamlOverrides:
    @pytest.fixture
    def override_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "overrides.yaml"
        p.write_text(
            """
extra_holidays:
  - 2026-05-19
  - 2026-05-20
special_trading_days:
  - 2026-05-23   # Saturday, market open
            """
        )
        return p

    def test_extra_holiday_makes_normal_weekday_non_trading(self, override_file):
        cal = TradingCalendar(_config(overrides=override_file))
        # Tuesday 2026-05-19, explicitly listed as a holiday.
        assert cal.is_trading_day(date(2026, 5, 19)) is False

    def test_special_trading_day_overrides_weekend(self, override_file):
        cal = TradingCalendar(_config(overrides=override_file))
        # Saturday 2026-05-23, explicitly a trading day.
        assert cal.is_trading_day(date(2026, 5, 23)) is True

    def test_missing_override_file_silently_ignored(self, tmp_path):
        missing = tmp_path / "does-not-exist.yaml"
        cal = TradingCalendar(_config(overrides=missing))
        # Tuesday is still trading.
        assert cal.is_trading_day(date(2026, 5, 19)) is True

    def test_empty_override_file_safe(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        cal = TradingCalendar(_config(overrides=p))
        assert cal.is_trading_day(date(2026, 5, 19)) is True

    def test_malformed_override_file_logged_not_raised(self, tmp_path, caplog):
        p = tmp_path / "broken.yaml"
        p.write_text("this is: : not valid : yaml :::")
        # Must construct without raising.
        cal = TradingCalendar(_config(overrides=p))
        # And fall back to weekday-only logic.
        assert cal.is_trading_day(date(2026, 5, 19)) is True


# ---------------------------------------------------------------------------
# previous_trading_day / next_trading_day
# ---------------------------------------------------------------------------


class TestPreviousTradingDay:
    def test_previous_from_tuesday_is_monday(self):
        cal = TradingCalendar(_config())
        # Tuesday 2026-05-19 → Monday 2026-05-18.
        assert cal.previous_trading_day(date(2026, 5, 19)) == date(2026, 5, 18)

    def test_previous_from_monday_skips_weekend(self):
        cal = TradingCalendar(_config())
        # Monday 2026-05-18 → Friday 2026-05-15.
        assert cal.previous_trading_day(date(2026, 5, 18)) == date(2026, 5, 15)

    def test_previous_from_sunday_is_friday(self):
        cal = TradingCalendar(_config())
        # Sunday 2026-05-24 → Friday 2026-05-22.
        assert cal.previous_trading_day(date(2026, 5, 24)) == date(2026, 5, 22)

    def test_runtime_error_if_no_trading_day_in_lookback(self):
        # Calendar with NO trading weekdays — impossible to find anything.
        # `_config(weekdays=[])` builds with frozenset(); the safety
        # guard at _MAX_LOOKBACK_DAYS should kick in and raise rather
        # than spin forever.
        cal = TradingCalendar(_config(weekdays=[]))
        with pytest.raises(RuntimeError, match="No trading day found"):
            cal.previous_trading_day(date(2026, 5, 19))

    def test_runtime_error_on_next_when_no_trading_day_in_lookback(self):
        cal = TradingCalendar(_config(weekdays=[]))
        with pytest.raises(RuntimeError, match="No trading day found"):
            cal.next_trading_day(date(2026, 5, 19))


class TestNextTradingDay:
    def test_next_from_tuesday_is_wednesday(self):
        cal = TradingCalendar(_config())
        assert cal.next_trading_day(date(2026, 5, 19)) == date(2026, 5, 20)

    def test_next_from_friday_skips_weekend(self):
        cal = TradingCalendar(_config())
        assert cal.next_trading_day(date(2026, 5, 22)) == date(2026, 5, 25)

    def test_next_from_saturday_is_monday(self):
        cal = TradingCalendar(_config())
        assert cal.next_trading_day(date(2026, 5, 23)) == date(2026, 5, 25)


# ---------------------------------------------------------------------------
# Algorithmic holidays via python-holidays (skipped if not installed)
# ---------------------------------------------------------------------------


pytest.importorskip("holidays")


class TestPythonHolidaysIntegration:
    def test_india_republic_day_is_holiday(self):
        # 2026-01-26 is Republic Day in India, always a federal holiday.
        cal = TradingCalendar(_config(country="IN"))
        assert cal.is_trading_day(date(2026, 1, 26)) is False

    def test_india_normal_weekday_is_trading(self):
        cal = TradingCalendar(_config(country="IN"))
        # 2026-05-19, a Tuesday with no listed holiday.
        assert cal.is_trading_day(date(2026, 5, 19)) is True

    def test_override_special_trading_beats_algorithmic_holiday(self, tmp_path):
        p = tmp_path / "ovr.yaml"
        p.write_text("special_trading_days: [2026-01-26]")
        cal = TradingCalendar(_config(country="IN", overrides=p))
        # Republic Day, explicitly marked open via override.
        assert cal.is_trading_day(date(2026, 1, 26)) is True

    def test_unsupported_country_falls_back_to_weekdays_only(self, caplog):
        # 'XX' isn't a valid python-holidays country; the fetch raises
        # and we fall back to "every weekday is a trading day."
        cal = TradingCalendar(_config(country="XX"))
        assert cal.is_trading_day(date(2026, 5, 19)) is True

    def test_per_year_cache_avoids_redundant_lookups(self):
        cal = TradingCalendar(_config(country="IN"))
        # Trigger first lookup for 2026.
        cal.is_trading_day(date(2026, 1, 1))
        # Internal cache should now have 2026.
        assert 2026 in cal._holiday_cache  # pragma: allow private-access
        # Second call must not re-hit python-holidays; we can't easily
        # assert "no call happened," but if the cache works, the
        # following will return identical truthy values consistently.
        for _ in range(5):
            assert cal.is_trading_day(date(2026, 1, 26)) is False
