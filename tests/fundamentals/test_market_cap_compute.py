"""Integration tests for compute_market_cap_for_date (Phase 4A).

These exercise the real Postgres join paths (ohlcv_bars × 
instrument_shares → daily_market_cap), so they need the full
db_engine fixture from tests/persistence/conftest.py.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.fundamentals.market_cap_compute import (
    ComputeResult,
    compute_market_cap_for_date,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    DailyMarketCapRepository,
    InstrumentSharesRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ---------- fixtures ---------- #


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def universe(db_engine, now):
    """Three NSE EQ + one BSE EQ + one NSE ETF (negative-control:
    ETF should NOT be touched)."""
    refs = [
        TickerRef("NSE", "RELIANCE", "EQ"),
        TickerRef("NSE", "TCS",      "EQ"),
        TickerRef("NSE", "HDFCBANK", "EQ"),
        TickerRef("BSE", "RELIANCE", "EQ"),
        TickerRef("NSE", "GOLDBEES", "EQ"),  # will be marked ETF below
    ]
    ids: dict[tuple, int] = {}
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        for ref in refs:
            ids[(ref.exchange, ref.symbol)] = repo.insert(ref, created_at=now)
        # Make GOLDBEES an ETF.
        conn.execute(text(
            "UPDATE tickers SET instrument_type='ETF' WHERE ticker_id=:tid"
        ), {"tid": ids[("NSE", "GOLDBEES")]})
    return ids


def _insert_bar(db_engine, ticker_id: int, dt: datetime, close: float):
    """Insert one 1d bar. open/high/low/volume use placeholder values
    since the compute logic only reads close."""
    with transaction(db_engine) as conn:
        conn.execute(text("""
            INSERT INTO ohlcv_bars
                (ticker_id, timeframe, bar_timestamp, open, high, low, close, volume)
            VALUES (:tid, '1d', :ts, :c, :c, :c, :c, 0.0)
        """), {"tid": ticker_id, "ts": dt, "c": close})


def _insert_shares(
    db_engine, ticker_id: int, as_of_date: date, count: int,
):
    with transaction(db_engine) as conn:
        InstrumentSharesRepository(conn).insert_snapshot(
            ticker_id, as_of_date=as_of_date,
            shares_outstanding=count, shares_source="YFINANCE",
        )


# ---------- happy path ---------- #


class TestComputeHappyPath:

    def test_all_three_NSE_tickers_get_COMPUTED_rows(self, db_engine, universe):
        """The base case: bars + shares both present → all 3 NSE EQ
        tickers get COMPUTED rows. BSE ticker untouched (different
        exchange). ETF untouched (instrument_type filter)."""
        td = date(2026, 5, 22)
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=timezone.utc)

        # Each NSE ticker gets a bar at td and a shares row dated
        # before-or-equal-to td.
        for sym, close, sh in [
            ("RELIANCE", 1450.0, 6_700_000_000),
            ("TCS",      4100.0, 3_600_000_000),
            ("HDFCBANK", 1700.0, 7_600_000_000),
        ]:
            _insert_bar(db_engine, universe[("NSE", sym)], bar_ts, close)
            _insert_shares(db_engine, universe[("NSE", sym)],
                           as_of_date=date(2026, 5, 21), count=sh)

        # BSE ticker also has a bar, but exchange filter should skip it.
        _insert_bar(db_engine, universe[("BSE", "RELIANCE")], bar_ts, 1450.0)
        _insert_shares(db_engine, universe[("BSE", "RELIANCE")],
                       as_of_date=date(2026, 5, 21), count=6_700_000_000)
        # ETF has bar + shares too — same negative-control reasoning.
        _insert_bar(db_engine, universe[("NSE", "GOLDBEES")], bar_ts, 60.0)
        _insert_shares(db_engine, universe[("NSE", "GOLDBEES")],
                       as_of_date=date(2026, 5, 21), count=1_000_000_000)

        result = compute_market_cap_for_date(db_engine, "NSE", td)
        assert result.succeeded_computed == 3
        assert result.succeeded_approx == 0
        assert result.skipped_no_close == 0
        assert result.skipped_no_shares == 0

        with db_engine.connect() as conn:
            r = DailyMarketCapRepository(conn)
            rows = r.list_for_date(td, exchange="NSE")
        # Three rows, none of them ETF or BSE.
        assert len(rows) == 3
        # Spot-check the math.
        ric = next(row for row in rows
                   if row.ticker_id == universe[("NSE", "RELIANCE")])
        assert ric.close_price == 1450.0
        assert ric.shares_used == 6_700_000_000
        assert ric.market_cap == pytest.approx(9_715_000_000_000.0)
        assert ric.source == "COMPUTED"

    def test_etf_is_excluded(self, db_engine, universe):
        """Even with bar + shares populated, the ETF should not get
        a mcap row — the universe query filters on
        instrument_type='EQ'."""
        td = date(2026, 5, 22)
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=timezone.utc)
        _insert_bar(db_engine, universe[("NSE", "GOLDBEES")], bar_ts, 60.0)
        _insert_shares(db_engine, universe[("NSE", "GOLDBEES")],
                       as_of_date=date(2026, 5, 21), count=1_000_000_000)
        compute_market_cap_for_date(db_engine, "NSE", td)
        with db_engine.connect() as conn:
            row = DailyMarketCapRepository(conn).get_at(
                universe[("NSE", "GOLDBEES")], td,
            )
        assert row is None

    def test_exchange_filter_is_respected(self, db_engine, universe):
        """compute for NSE should not touch BSE rows even if BSE has
        the same symbol present."""
        td = date(2026, 5, 22)
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=timezone.utc)
        _insert_bar(db_engine, universe[("BSE", "RELIANCE")], bar_ts, 1450.0)
        _insert_shares(db_engine, universe[("BSE", "RELIANCE")],
                       as_of_date=date(2026, 5, 21), count=6_700_000_000)
        compute_market_cap_for_date(db_engine, "NSE", td)
        with db_engine.connect() as conn:
            bse_row = DailyMarketCapRepository(conn).get_at(
                universe[("BSE", "RELIANCE")], td,
            )
        # BSE row should NOT have been created by an NSE compute.
        assert bse_row is None


# ---------- approx fallback ---------- #


class TestApproxFallback:
    """When shares aren't known for the trade_date but are known
    for a later date (today's shares), we either fall back to
    APPROX_TODAYS_SHARES (default) or skip (strict mode)."""

    def test_approx_fallback_writes_row(self, db_engine, universe):
        """trade_date is 2025-06-01; shares are only known from
        2026-05-21. Default behavior: write APPROX_TODAYS_SHARES."""
        td = date(2025, 6, 1)
        bar_ts = datetime(2025, 6, 1, 15, 30, tzinfo=timezone.utc)
        _insert_bar(db_engine, universe[("NSE", "RELIANCE")], bar_ts, 1300.0)
        # Shares as of LATER than the trade_date.
        _insert_shares(db_engine, universe[("NSE", "RELIANCE")],
                       as_of_date=date(2026, 5, 21), count=6_700_000_000)

        result = compute_market_cap_for_date(db_engine, "NSE", td)
        # Only RELIANCE was populated; the other 2 NSE EQ tickers
        # have no bars and no shares — they get skipped_no_close.
        assert result.succeeded_approx == 1
        assert result.succeeded_computed == 0
        assert result.skipped_no_close == 2  # TCS, HDFCBANK
        assert result.skipped_no_shares == 0

        with db_engine.connect() as conn:
            row = DailyMarketCapRepository(conn).get_at(
                universe[("NSE", "RELIANCE")], td,
            )
        assert row is not None
        assert row.source == "APPROX_TODAYS_SHARES"
        assert row.market_cap == pytest.approx(1300.0 * 6_700_000_000)

    def test_strict_mode_skips_instead(self, db_engine, universe):
        """allow_approx_fallback=False → no APPROX row, count goes
        to skipped_no_shares."""
        td = date(2025, 6, 1)
        bar_ts = datetime(2025, 6, 1, 15, 30, tzinfo=timezone.utc)
        _insert_bar(db_engine, universe[("NSE", "RELIANCE")], bar_ts, 1300.0)
        _insert_shares(db_engine, universe[("NSE", "RELIANCE")],
                       as_of_date=date(2026, 5, 21), count=6_700_000_000)

        result = compute_market_cap_for_date(
            db_engine, "NSE", td, allow_approx_fallback=False,
        )
        assert result.succeeded_approx == 0
        assert result.succeeded_computed == 0
        assert result.skipped_no_shares == 1
        assert result.skipped_no_close == 2

        with db_engine.connect() as conn:
            row = DailyMarketCapRepository(conn).get_at(
                universe[("NSE", "RELIANCE")], td,
            )
        assert row is None

    def test_exact_date_match_prefers_COMPUTED(self, db_engine, universe):
        """When shares ARE known for the trade_date, source must be
        COMPUTED — not APPROX even though fallback is allowed."""
        td = date(2026, 5, 22)
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=timezone.utc)
        _insert_bar(db_engine, universe[("NSE", "RELIANCE")], bar_ts, 1450.0)
        _insert_shares(db_engine, universe[("NSE", "RELIANCE")],
                       as_of_date=date(2026, 5, 22), count=6_700_000_000)
        compute_market_cap_for_date(db_engine, "NSE", td)
        with db_engine.connect() as conn:
            row = DailyMarketCapRepository(conn).get_at(
                universe[("NSE", "RELIANCE")], td,
            )
        assert row.source == "COMPUTED"


# ---------- skip cases ---------- #


class TestSkipCases:

    def test_no_close_skips_silently(self, db_engine, universe):
        """Ticker has shares but no bar for the date → skipped_no_close."""
        td = date(2026, 5, 22)
        _insert_shares(db_engine, universe[("NSE", "RELIANCE")],
                       as_of_date=date(2026, 5, 21), count=6_700_000_000)
        # No bar inserted for RELIANCE on this date.
        result = compute_market_cap_for_date(db_engine, "NSE", td)
        assert result.succeeded_computed == 0
        assert result.skipped_no_close == 3  # all 3 NSE EQ tickers

    def test_no_shares_at_all_skips(self, db_engine, universe):
        """Ticker has a bar but no shares anywhere → skipped_no_shares."""
        td = date(2026, 5, 22)
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=timezone.utc)
        _insert_bar(db_engine, universe[("NSE", "RELIANCE")], bar_ts, 1450.0)
        # No shares insert.
        result = compute_market_cap_for_date(db_engine, "NSE", td)
        assert result.skipped_no_shares == 1
        # The other 2 NSE EQ tickers don't have bars either.
        assert result.skipped_no_close == 2

    def test_empty_universe_returns_zero_result(self, db_engine):
        """No active tickers on an exchange → all counts zero,
        no crash."""
        result = compute_market_cap_for_date(db_engine, "BSE", date(2026, 5, 22))
        assert result == ComputeResult(
            trade_date=date(2026, 5, 22), exchange="BSE",
            succeeded_computed=0, succeeded_approx=0,
            skipped_no_close=0, skipped_no_shares=0,
        )


# ---------- idempotency ---------- #


class TestIdempotency:

    def test_rerun_overwrites_with_new_data(self, db_engine, universe):
        """If the operator re-runs the compute for a date (e.g.
        because shares were corrected), the new values should
        overwrite the old. Tests the repo's upsert semantics in
        the compute path."""
        td = date(2026, 5, 22)
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=timezone.utc)
        _insert_bar(db_engine, universe[("NSE", "RELIANCE")], bar_ts, 1400.0)
        _insert_shares(db_engine, universe[("NSE", "RELIANCE")],
                       as_of_date=date(2026, 5, 21), count=1_000_000_000)
        compute_market_cap_for_date(db_engine, "NSE", td)

        # Now correct the shares — operator updates the snapshot.
        _insert_shares(db_engine, universe[("NSE", "RELIANCE")],
                       as_of_date=date(2026, 5, 21), count=6_700_000_000)
        compute_market_cap_for_date(db_engine, "NSE", td)

        with db_engine.connect() as conn:
            row = DailyMarketCapRepository(conn).get_at(
                universe[("NSE", "RELIANCE")], td,
            )
        # New shares_used; new market_cap.
        assert row.shares_used == 6_700_000_000
        assert row.market_cap == pytest.approx(1400.0 * 6_700_000_000)
