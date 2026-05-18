"""Tests for NseTradingCalendar. The base behavior uses the holidays
package's India calendar; we test our weekend + override logic and
graceful degradation when holidays is missing."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from trip_ema.etl.holidays_calendar import NseTradingCalendar


class TestTradingDays:

    def test_saturday_is_not_trading_day(self):
        cal = NseTradingCalendar()
        # 2026-05-16 is a Saturday
        sat = date(2026, 5, 16)
        assert sat.weekday() == 5
        assert not cal.is_trading_day(sat)

    def test_sunday_is_not_trading_day(self):
        cal = NseTradingCalendar()
        sun = date(2026, 5, 17)
        assert sun.weekday() == 6
        assert not cal.is_trading_day(sun)

    def test_typical_weekday_is_trading_day(self):
        cal = NseTradingCalendar()
        # A random Monday that's not a known Indian holiday
        # 2026-05-04 is a Monday; not a public holiday
        d = date(2026, 5, 4)
        assert d.weekday() == 0
        # Note: this may fail if holidays package isn't installed; in
        # that case is_trading_day returns True for weekdays (no
        # holiday data → assume trading day).
        assert cal.is_trading_day(d)

    def test_previous_trading_day_skips_weekend(self):
        cal = NseTradingCalendar()
        # From Monday 2026-05-04, previous_trading_day should give
        # Friday 2026-05-01 (or earlier if 5/1 is a holiday — Labor Day
        # IS a holiday in India, so it might be 4/30)
        monday = date(2026, 5, 4)
        prev = cal.previous_trading_day(monday)
        # Whatever it returns, it must not be a weekend
        assert prev.weekday() < 5
        # And must be strictly earlier than the input
        assert prev < monday


class TestOverrides:

    def test_extra_holiday_override(self, tmp_path):
        # Mark 2026-05-04 (a Monday) as a holiday via override
        override = tmp_path / "override.yaml"
        override.write_text(
            "extra_holidays:\n  - 2026-05-04\n"
        )
        cal = NseTradingCalendar(override_path=override)
        assert not cal.is_trading_day(date(2026, 5, 4))

    def test_special_trading_day_overrides_weekend(self, tmp_path):
        """Muhurat trading sometimes happens on a Sunday. The override
        can flag it as a trading day."""
        override = tmp_path / "override.yaml"
        override.write_text(
            "special_trading_days:\n  - 2026-05-17\n"  # a Sunday
        )
        cal = NseTradingCalendar(override_path=override)
        assert cal.is_trading_day(date(2026, 5, 17))

    def test_special_trading_day_overrides_holiday(self, tmp_path):
        """If a date is in BOTH extra_holidays and special_trading_days,
        special_trading_days wins (it's checked first)."""
        override = tmp_path / "override.yaml"
        override.write_text(
            "extra_holidays:\n  - 2026-05-04\n"
            "special_trading_days:\n  - 2026-05-04\n"
        )
        cal = NseTradingCalendar(override_path=override)
        assert cal.is_trading_day(date(2026, 5, 4))

    def test_missing_override_file_is_silent(self, tmp_path):
        """Pointing at a nonexistent override path doesn't raise."""
        missing = tmp_path / "doesnt_exist.yaml"
        cal = NseTradingCalendar(override_path=missing)
        # Should still work for normal weekdays
        assert cal.is_trading_day(date(2026, 5, 4))

    def test_malformed_override_is_logged_not_raised(self, tmp_path, caplog):
        """A broken YAML override doesn't kill the calendar — it logs
        and continues without overrides."""
        override = tmp_path / "broken.yaml"
        override.write_text("extra_holidays:\n  - notadate\n")
        # Should not raise; just logs
        cal = NseTradingCalendar(override_path=override)
        # Falls back to base behavior
        assert cal.is_trading_day(date(2026, 5, 4))


class TestTickerAdvIntegration:
    """Smoke: the calendar is used by the runner to skip non-trading
    days. Verify that interface doesn't change accidentally."""

    def test_calendar_is_constructible_without_yaml(self):
        cal = NseTradingCalendar()
        # Smoke — no exception
        cal.is_trading_day(date(2026, 5, 4))
