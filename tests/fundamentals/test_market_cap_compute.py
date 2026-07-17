"""Integration tests for compute_market_cap_for_date (Phase 4A).

These exercise the real Postgres join paths (ohlcv_bars × 
instrument_shares → daily_market_cap), so they need the full
db_engine fixture from tests/persistence/conftest.py.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, UTC

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
    return datetime.now(UTC).replace(microsecond=0)


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
            VALUES (:tid, '1D', :ts, :c, :c, :c, :c, 0.0)
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

    def test_stocks_filter_scopes_universe_without_bind_error(
        self, db_engine, universe,
    ):
        """Regression: compute_market_cap_for_date(stocks=[...]) must run
        the parameterized universe filter (the :: cast form collided with
        :param -> psycopg2 ProgrammingError) AND restrict rows to exactly
        the given tickers."""
        td = date(2026, 5, 22)
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=UTC)
        for sym, close, sh in [
            ("RELIANCE", 1450.0, 6_700_000_000),
            ("TCS", 4100.0, 3_600_000_000),
        ]:
            _insert_bar(db_engine, universe[("NSE", sym)], bar_ts, close)
            _insert_shares(db_engine, universe[("NSE", sym)],
                           as_of_date=date(2026, 5, 21), count=sh)
        only = universe[("NSE", "RELIANCE")]
        result = compute_market_cap_for_date(
            db_engine, "NSE", td,
            use_current_shares_always=True, stocks=[only],
        )
        with db_engine.connect() as conn:
            ids = [r[0] for r in conn.execute(text(
                "SELECT ticker_id FROM daily_market_cap "
                "WHERE trade_date = :td ORDER BY ticker_id"
            ), {"td": td}).fetchall()]
        assert ids == [only]
        assert result.succeeded_approx == 1

    def test_stocks_none_is_unscoped(self, db_engine, universe):
        """stocks=None must be byte-identical to the all-universe path."""
        td = date(2026, 5, 22)
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=UTC)
        for sym, close, sh in [
            ("RELIANCE", 1450.0, 6_700_000_000),
            ("TCS", 4100.0, 3_600_000_000),
        ]:
            _insert_bar(db_engine, universe[("NSE", sym)], bar_ts, close)
            _insert_shares(db_engine, universe[("NSE", sym)],
                           as_of_date=date(2026, 5, 21), count=sh)
        compute_market_cap_for_date(
            db_engine, "NSE", td,
            use_current_shares_always=True, stocks=None,
        )
        with db_engine.connect() as conn:
            n = conn.execute(text(
                "SELECT count(*) FROM daily_market_cap WHERE trade_date = :td"
            ), {"td": td}).scalar()
        assert n == 2

    def test_shares_override_matches_per_ticker_current_shares(
        self, db_engine, universe,
    ):
        """The range-walk fast path (shares_override) must produce rows
        byte-identical to the per-ticker current-shares resolution."""
        td = date(2026, 5, 22)
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=UTC)
        for sym, close, sh in [
            ("RELIANCE", 1450.0, 6_700_000_000),
            ("TCS", 4100.0, 3_600_000_000),
            ("HDFCBANK", 1700.0, 7_600_000_000),
        ]:
            _insert_bar(db_engine, universe[("NSE", sym)], bar_ts, close)
            _insert_shares(db_engine, universe[("NSE", sym)],
                           as_of_date=date(2026, 5, 21), count=sh)

        def _snapshot():
            with db_engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT ticker_id, market_cap, shares_used, source "
                    "FROM daily_market_cap WHERE trade_date = :td "
                    "ORDER BY ticker_id"
                ), {"td": td}).fetchall()
            return [(r[0], float(r[1]), r[2], r[3]) for r in rows]

        # Per-ticker current-shares path.
        r1 = compute_market_cap_for_date(
            db_engine, "NSE", td, use_current_shares_always=True,
        )
        snap1 = _snapshot()

        # Clear, then re-run via a shares_override built the way
        # populate_market_cap_range builds it.
        with transaction(db_engine) as conn:
            conn.execute(
                text("DELETE FROM daily_market_cap WHERE trade_date = :td"),
                {"td": td},
            )
            latest = InstrumentSharesRepository(conn).list_latest_for_exchange(
                "NSE",
            )
        override = {
            s.ticker_id: (s.shares_outstanding, "APPROX_TODAYS_SHARES")
            for s in latest
            if s.shares_outstanding is not None
        }
        r2 = compute_market_cap_for_date(
            db_engine, "NSE", td,
            use_current_shares_always=True, shares_override=override,
        )
        snap2 = _snapshot()

        assert r1.succeeded_approx == r2.succeeded_approx == 3
        assert r1.succeeded_computed == r2.succeeded_computed == 0
        assert snap1 == snap2

    def test_upsert_snapshot_many_batches_and_updates(
        self, db_engine, universe,
    ):
        """upsert_snapshot_many writes all rows in one call and updates
        (not duplicates) on re-run — idempotent on (ticker_id, td)."""
        td = date(2026, 5, 22)
        ids = [universe[("NSE", s)] for s in ("RELIANCE", "TCS", "HDFCBANK")]
        rows = [
            {"ticker_id": tid, "trade_date": td, "close_price": 100.0 + i,
             "shares_used": 1_000_000, "market_cap": (100.0 + i) * 1_000_000,
             "source": "APPROX_TODAYS_SHARES"}
            for i, tid in enumerate(ids)
        ]
        with transaction(db_engine) as conn:
            n = DailyMarketCapRepository(conn).upsert_snapshot_many(rows)
        assert n == 3
        with db_engine.connect() as conn:
            got = conn.execute(text(
                "SELECT COUNT(*) FROM daily_market_cap WHERE trade_date = :td"
            ), {"td": td}).scalar()
        assert got == 3

        # Re-run with a changed close -> update in place, no duplicate row.
        rows[0]["close_price"] = 999.0
        rows[0]["market_cap"] = 999.0 * 1_000_000
        with transaction(db_engine) as conn:
            DailyMarketCapRepository(conn).upsert_snapshot_many(rows)
        with db_engine.connect() as conn:
            got2 = conn.execute(text(
                "SELECT COUNT(*) FROM daily_market_cap WHERE trade_date = :td"
            ), {"td": td}).scalar()
            price = conn.execute(text(
                "SELECT close_price FROM daily_market_cap "
                "WHERE ticker_id = :t AND trade_date = :td"
            ), {"t": ids[0], "td": td}).scalar()
        assert got2 == 3
        assert price == pytest.approx(999.0)

    def test_upsert_snapshot_many_empty_is_noop(self, db_engine):
        with transaction(db_engine) as conn:
            assert DailyMarketCapRepository(conn).upsert_snapshot_many([]) == 0

    def test_upsert_snapshot_many_validates_before_writing(
        self, db_engine, universe,
    ):
        """A single invalid row aborts the whole batch — nothing lands."""
        td = date(2026, 5, 22)
        good = universe[("NSE", "RELIANCE")]
        rows = [
            {"ticker_id": good, "trade_date": td, "close_price": 100.0,
             "shares_used": 1_000_000, "market_cap": 100_000_000.0,
             "source": "APPROX_TODAYS_SHARES"},
            {"ticker_id": good, "trade_date": td, "close_price": 100.0,
             "shares_used": 1_000_000, "market_cap": -1.0,  # invalid
             "source": "APPROX_TODAYS_SHARES"},
        ]
        with pytest.raises(ValueError):
            with transaction(db_engine) as conn:
                DailyMarketCapRepository(conn).upsert_snapshot_many(rows)
        with db_engine.connect() as conn:
            got = conn.execute(text(
                "SELECT COUNT(*) FROM daily_market_cap WHERE trade_date = :td"
            ), {"td": td}).scalar()
        assert got == 0

    def test_populate_range_parallel_aggregates_all_days(
        self, db_engine, universe,
    ):
        """populate_market_cap_range computes every trading day (in
        parallel) and aggregates the per-day counters correctly, with
        no lost rows across workers."""
        from trip_ema.fundamentals.market_cap_compute import (
            populate_market_cap_range,
        )
        days = [date(2026, 5, 20), date(2026, 5, 21), date(2026, 5, 22)]
        for d in days:
            ts = datetime(d.year, d.month, d.day, 15, 30, tzinfo=UTC)
            for sym, close in [
                ("RELIANCE", 1450.0), ("TCS", 4100.0), ("HDFCBANK", 1700.0),
            ]:
                _insert_bar(db_engine, universe[("NSE", sym)], ts, close)
        for sym, sh in [
            ("RELIANCE", 6_700_000_000), ("TCS", 3_600_000_000),
            ("HDFCBANK", 7_600_000_000),
        ]:
            _insert_shares(db_engine, universe[("NSE", sym)],
                           as_of_date=date(2026, 5, 1), count=sh)

        total = populate_market_cap_range(
            db_engine, "NSE",
            date_from=days[0], date_to=days[-1],
            use_current_shares_always=True,
        )
        assert total["days"] == 3
        assert total["approx"] == 9      # 3 tickers x 3 days
        assert total["computed"] == 0
        with db_engine.connect() as conn:
            got = conn.execute(
                text("SELECT COUNT(*) FROM daily_market_cap"),
            ).scalar()
        assert got == 9

    def test_all_three_NSE_tickers_get_COMPUTED_rows(self, db_engine, universe):
        """The base case: bars + shares both present → all 3 NSE EQ
        tickers get COMPUTED rows. BSE ticker untouched (different
        exchange). ETF untouched (instrument_type filter)."""
        td = date(2026, 5, 22)
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=UTC)

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
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=UTC)
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
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=UTC)
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
        bar_ts = datetime(2025, 6, 1, 15, 30, tzinfo=UTC)
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
        bar_ts = datetime(2025, 6, 1, 15, 30, tzinfo=UTC)
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
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=UTC)
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
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=UTC)
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
        bar_ts = datetime(2026, 5, 22, 15, 30, tzinfo=UTC)
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
