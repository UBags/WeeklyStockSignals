"""The market-cap day window selects THE day, not the day after it.

This test exists because compute_market_cap_for_date built its window from naive
datetimes, Postgres coerced them using the session timezone, and the result was a
window that contained the NEXT trading day's bar and not the current one. Every
daily market cap was computed from tomorrow's close — lookahead, on the signal
path, feeding constituent selection and therefore every industry index.

The market-cap fixtures did not catch it because they still seeded bars at midnight
UTC, the pre-anchor convention: the tests agreed with themselves. So this one seeds
through bar_anchor() — the production writer's own primitive — and gives D and D+1
deliberately different closes, so a window that is off by a day cannot produce the
right number by luck.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from trip_ema.domain import OHLCVBar, TickerRef, Timeframe
from trip_ema.domain.market_session import bar_anchor
from trip_ema.fundamentals.market_cap_compute import compute_market_cap_for_date
from trip_ema.persistence import transaction
from trip_ema.persistence.bars_repo import BarsRepository
from trip_ema.persistence.fundamentals_repo import InstrumentSharesRepository
from trip_ema.persistence.tickers_repo import TickersRepository


D0 = date(2026, 7, 13)          # a Monday
D1 = date(2026, 7, 14)

CLOSE_D0 = 100.0
CLOSE_D1 = 999.0                # deliberately nothing like D0's
SHARES = 1_000_000


@pytest.fixture
def ticker_with_two_days(db_engine):
    now = datetime.now(UTC)
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(
            TickerRef("NSE", "MCAPWIN", "EQ"), created_at=now,
        )
        bars = BarsRepository(conn)
        for d, close in ((D0, CLOSE_D0), (D1, CLOSE_D1)):
            bars.upsert_bar(
                tid, Timeframe.DAILY,
                OHLCVBar(
                    timestamp=bar_anchor(d, "NSE"),
                    open=close, high=close, low=close, close=close,
                    volume=1000.0,
                ),
                adj_close=close,
            )
        InstrumentSharesRepository(conn).insert_snapshot(
            tid, as_of_date=D0, shares_outstanding=SHARES,
            shares_source="YFINANCE",
        )
    return tid


class TestDayWindow:

    def test_market_cap_for_a_day_uses_that_days_close(
        self, db_engine, ticker_with_two_days,
    ):
        """The lookahead test. Before the fix this returned D+1's close x shares."""
        result = compute_market_cap_for_date(db_engine, "NSE", D0)
        assert result.succeeded_computed + result.succeeded_approx == 1

        with db_engine.connect() as conn:
            row = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT close_price, market_cap FROM daily_market_cap "
                    " WHERE ticker_id = :t AND trade_date = :d"
                ),
                {"t": ticker_with_two_days, "d": D0},
            ).one()

        assert float(row.close_price) == pytest.approx(CLOSE_D0), (
            f"market cap for {D0} was computed from a close of {row.close_price}; "
            f"{CLOSE_D1} is the NEXT day's close — the window is a day out"
        )
        assert float(row.market_cap) == pytest.approx(CLOSE_D0 * SHARES)

    def test_the_next_day_gets_its_own_close(
        self, db_engine, ticker_with_two_days,
    ):
        compute_market_cap_for_date(db_engine, "NSE", D1)
        with db_engine.connect() as conn:
            row = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT close_price FROM daily_market_cap "
                    " WHERE ticker_id = :t AND trade_date = :d"
                ),
                {"t": ticker_with_two_days, "d": D1},
            ).one()
        assert float(row.close_price) == pytest.approx(CLOSE_D1)

    def test_a_day_with_no_bar_computes_nothing(
        self, db_engine, ticker_with_two_days,
    ):
        """The other half of an off-by-one window: it must not reach BACKWARD
        either. D0 - 1 has no bar; the answer is 'no close', not D0's."""
        result = compute_market_cap_for_date(
            db_engine, "NSE", D0 - timedelta(days=1),
        )
        assert result.succeeded_computed + result.succeeded_approx == 0
        assert result.skipped_no_close == 1
