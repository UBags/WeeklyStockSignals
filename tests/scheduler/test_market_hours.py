"""Tests for MarketHoursService."""

from __future__ import annotations

from datetime import datetime, timezone, UTC

import pytest

from trip_ema.config.schema import MarketHoursConfig
from trip_ema.scheduler.market_hours import MarketHoursService


def _nse_config() -> MarketHoursConfig:
    return MarketHoursConfig(
        exchange="NSE", open="09:15", close="15:30",
        tz="Asia/Kolkata", weekdays=[1, 2, 3, 4, 5],
    )


def _nyse_config() -> MarketHoursConfig:
    return MarketHoursConfig(
        exchange="NYSE", open="09:30", close="16:00",
        tz="America/New_York", weekdays=[1, 2, 3, 4, 5],
    )


# ---------- Naive timestamps rejected ---------- #

def test_naive_datetime_rejected():
    svc = MarketHoursService([_nse_config()])
    naive = datetime(2026, 5, 18, 10, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        svc.is_open("NSE", naive)


def test_unknown_exchange_raises():
    svc = MarketHoursService([_nse_config()])
    when = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    with pytest.raises(KeyError):
        svc.is_open("FAKE", when)


# ---------- NSE: Monday during trading hours ---------- #

def test_nse_open_at_10am_ist_on_monday():
    svc = MarketHoursService([_nse_config()])
    # 10:00 IST on Monday 2026-05-18 == 04:30 UTC
    when = datetime(2026, 5, 18, 4, 30, tzinfo=UTC)
    assert svc.is_open("NSE", when) is True


def test_nse_closed_before_market_open():
    svc = MarketHoursService([_nse_config()])
    # 08:00 IST Monday = 02:30 UTC, before 09:15 open
    when = datetime(2026, 5, 18, 2, 30, tzinfo=UTC)
    assert svc.is_open("NSE", when) is False


def test_nse_closed_after_market_close():
    svc = MarketHoursService([_nse_config()])
    # 16:00 IST Monday = 10:30 UTC, after 15:30 close
    when = datetime(2026, 5, 18, 10, 30, tzinfo=UTC)
    assert svc.is_open("NSE", when) is False


def test_nse_closed_on_saturday():
    svc = MarketHoursService([_nse_config()])
    # 11:00 IST Saturday 2026-05-23 = 05:30 UTC
    when = datetime(2026, 5, 23, 5, 30, tzinfo=UTC)
    assert svc.is_open("NSE", when) is False


def test_nse_closed_on_sunday():
    svc = MarketHoursService([_nse_config()])
    when = datetime(2026, 5, 24, 5, 30, tzinfo=UTC)
    assert svc.is_open("NSE", when) is False


# ---------- Boundary conditions ---------- #

def test_open_inclusive_close_exclusive():
    """09:15 IST → open. 15:30 IST → closed (close-time is exclusive)."""
    svc = MarketHoursService([_nse_config()])
    # 09:15 IST Monday = 03:45 UTC
    at_open = datetime(2026, 5, 18, 3, 45, tzinfo=UTC)
    assert svc.is_open("NSE", at_open) is True
    # 15:30 IST Monday = 10:00 UTC
    at_close = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    assert svc.is_open("NSE", at_close) is False


# ---------- Multiple exchanges, any_open ---------- #

def test_any_open_when_nyse_alone_is_open():
    """No exchanges in Asia open, NYSE open."""
    svc = MarketHoursService([_nse_config(), _nyse_config()])
    # 14:00 ET Monday = 18:00 UTC
    when = datetime(2026, 5, 18, 18, 0, tzinfo=UTC)
    assert svc.is_open("NSE", when) is False
    assert svc.is_open("NYSE", when) is True
    assert svc.any_open(when) is True


def test_any_open_when_all_exchanges_closed():
    svc = MarketHoursService([_nse_config(), _nyse_config()])
    # 02:00 UTC on a Monday — NSE not yet open (07:30 IST), NYSE closed
    when = datetime(2026, 5, 18, 2, 0, tzinfo=UTC)
    assert svc.any_open(when) is False


def test_known_exchanges_returns_all_configured():
    svc = MarketHoursService([_nse_config(), _nyse_config()])
    assert sorted(svc.known_exchanges()) == ["NSE", "NYSE"]
