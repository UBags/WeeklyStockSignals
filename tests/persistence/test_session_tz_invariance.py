"""The queries must return the same answer on any session timezone.

Postgres evaluates `timestamptz::date` and `CURRENT_DATE` in the SESSION timezone.
This box's server TZ is Asia/Kolkata, which is why a codebase full of implicit
casts appeared to work: they were all reading a clock nobody chose, and it happened
to be the right one.

These tests run the real queries through engines pinned to three different session
timezones and assert the answers are identical. Anything that depends on the
session clock fails here — which is the only place a wrong trading date is cheap.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine

from trip_ema.domain import OHLCVBar, TickerRef, Timeframe
from trip_ema.domain.market_session import bar_anchor
from trip_ema.fundamentals.market_cap_compute import _trading_days_from_bars
from trip_ema.persistence import transaction
from trip_ema.persistence.backfill_queries import min_bar_date
from trip_ema.persistence.bars_repo import BarsRepository
from trip_ema.persistence.tickers_repo import TickersRepository


SESSION_ZONES = ["UTC", "Asia/Kolkata", "America/New_York"]

# Deliberately a MONDAY. The failure this suite exists to prevent puts a Monday
# bar in the previous week, and Monday is the only day on which that is visible.
TRADING_DAYS = [date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15)]


def _engine_in_zone(db_engine, tz: str):
    """The same database, seen through a session pinned to `tz`."""
    return create_engine(
        db_engine.url, connect_args={"options": f"-c timezone={tz}"},
    )


@pytest.fixture
def seeded(db_engine):
    now = datetime.now(UTC)
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(
            TickerRef("NSE", "TZPROBE", "EQ"), created_at=now,
        )
        repo = BarsRepository(conn)
        for i, d in enumerate(TRADING_DAYS):
            repo.upsert_bar(
                tid, Timeframe.DAILY,
                OHLCVBar(
                    timestamp=bar_anchor(d, "NSE"),
                    open=100.0 + i, high=101.0 + i,
                    low=99.0 + i, close=100.5 + i, volume=1000.0,
                ),
            )
    return tid


class TestSessionTimezoneInvariance:

    def test_min_bar_date(self, db_engine, seeded):
        answers = set()
        for tz in SESSION_ZONES:
            eng = _engine_in_zone(db_engine, tz)
            with eng.connect() as conn:
                answers.add(min_bar_date(conn, exchange="NSE"))
            eng.dispose()
        assert answers == {TRADING_DAYS[0]}, (
            f"min_bar_date changes answer with the session TZ: {answers}"
        )

    def test_trading_days_from_bars(self, db_engine, seeded):
        answers = set()
        for tz in SESSION_ZONES:
            eng = _engine_in_zone(db_engine, tz)
            answers.add(tuple(_trading_days_from_bars(
                eng, "NSE", TRADING_DAYS[0], TRADING_DAYS[-1],
            )))
            eng.dispose()
        assert answers == {tuple(TRADING_DAYS)}, (
            f"the trading-day list changes with the session TZ: {answers}"
        )

    def test_the_boundary_days_are_not_lost(self, db_engine, seeded):
        """A half-open, market-local window must include BOTH edges of the range.
        A UTC-midnight lower bound sits 5h30m after the anchor it is meant to
        include, and silently drops the first day."""
        for tz in SESSION_ZONES:
            eng = _engine_in_zone(db_engine, tz)
            days = _trading_days_from_bars(
                eng, "NSE", TRADING_DAYS[0], TRADING_DAYS[-1],
            )
            eng.dispose()
            assert days[0] == TRADING_DAYS[0], f"first day lost under {tz}"
            assert days[-1] == TRADING_DAYS[-1], f"last day lost under {tz}"


class TestAnchorSurvivesTheRoundTrip:

    def test_written_anchor_reads_back_as_the_same_trading_date(
        self, db_engine, seeded,
    ):
        """The property the whole rule exists for: what goes in comes out, on any
        session clock."""
        from trip_ema.domain.market_session import trading_date_of

        for tz in SESSION_ZONES:
            eng = _engine_in_zone(db_engine, tz)
            with eng.connect() as conn:
                bars = BarsRepository(conn).fetch_history(
                    seeded, Timeframe.DAILY, limit=10,
                )
            eng.dispose()
            got = [trading_date_of(b.timestamp, "NSE") for b in bars]
            assert got == TRADING_DAYS, f"round trip broke under session TZ {tz}"
