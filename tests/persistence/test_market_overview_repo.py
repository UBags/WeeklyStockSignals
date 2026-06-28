"""Tests for MarketOverviewRepository (Step 13b).

The repo backs the ETL Status GUI's Market Overview footer +
the 90-day trend dialogs. Read-only methods over
market_overview_daily.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, UTC

import pytest
from sqlalchemy import text

from trip_ema.persistence import transaction
from trip_ema.persistence.market_overview_repo import (
    MarketOverviewRepository,
    MarketOverviewRow,
)


def _seed_row(
    conn,
    *,
    snapshot_date: date,
    exchange: str = "NSE",
    n_buys: int = 0,
    n_sells: int = 0,
    n_exit_buys: int = 0,
    n_exit_sells: int = 0,
    n_green_regime: int = 0,
    n_red_regime: int = 0,
    n_neutral_regime: int = 0,
    n_above_highest_ema: int = 0,
    n_below_lowest_ema: int = 0,
    n_between_emas: int = 0,
):
    """Direct INSERT — no UPSERT logic, tests can rely on
    deterministic state."""
    conn.execute(text("""
        INSERT INTO market_overview_daily (
            snapshot_date, exchange,
            n_buys, n_sells, n_exit_buys, n_exit_sells,
            n_green_regime, n_red_regime, n_neutral_regime,
            n_above_highest_ema, n_below_lowest_ema, n_between_emas
        ) VALUES (
            :d, :ex, :b, :s, :eb, :es,
            :g, :r, :n, :ah, :bl, :bt
        )
    """), {
        "d": snapshot_date, "ex": exchange,
        "b": n_buys, "s": n_sells,
        "eb": n_exit_buys, "es": n_exit_sells,
        "g": n_green_regime, "r": n_red_regime, "n": n_neutral_regime,
        "ah": n_above_highest_ema, "bl": n_below_lowest_ema,
        "bt": n_between_emas,
    })


class TestLatest:

    def test_returns_none_when_empty(self, db_engine):
        with db_engine.connect() as conn:
            row = MarketOverviewRepository(conn).latest(
                exchange="NSE",
            )
        assert row is None

    def test_returns_most_recent_when_one_row(self, db_engine):
        d = date(2026, 6, 28)
        with transaction(db_engine) as conn:
            _seed_row(conn, snapshot_date=d, n_buys=14)
        with db_engine.connect() as conn:
            row = MarketOverviewRepository(conn).latest(
                exchange="NSE",
            )
        assert row is not None
        assert row.snapshot_date == d
        assert row.n_buys == 14

    def test_picks_latest_when_multiple_rows(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_row(
                conn, snapshot_date=date(2026, 6, 26), n_buys=1,
            )
            _seed_row(
                conn, snapshot_date=date(2026, 6, 28), n_buys=99,
            )
            _seed_row(
                conn, snapshot_date=date(2026, 6, 27), n_buys=2,
            )
        with db_engine.connect() as conn:
            row = MarketOverviewRepository(conn).latest(
                exchange="NSE",
            )
        assert row.snapshot_date == date(2026, 6, 28)
        assert row.n_buys == 99

    def test_filter_by_exchange(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_row(
                conn, snapshot_date=date(2026, 6, 28),
                exchange="NSE", n_buys=10,
            )
            _seed_row(
                conn, snapshot_date=date(2026, 6, 28),
                exchange="BSE", n_buys=20,
            )
        with db_engine.connect() as conn:
            nse = MarketOverviewRepository(conn).latest(
                exchange="NSE",
            )
            bse = MarketOverviewRepository(conn).latest(
                exchange="BSE",
            )
        assert nse.n_buys == 10
        assert bse.n_buys == 20


class TestRange:

    def test_empty_when_no_rows(self, db_engine):
        with db_engine.connect() as conn:
            rows = MarketOverviewRepository(conn).range(
                exchange="NSE", n_days=90,
            )
        assert rows == []

    def test_oldest_first_ordering(self, db_engine):
        with transaction(db_engine) as conn:
            for offset in (5, 1, 3, 0, 2, 4):
                _seed_row(
                    conn,
                    snapshot_date=date.today() - timedelta(days=offset),
                    n_buys=offset,
                )
        with db_engine.connect() as conn:
            rows = MarketOverviewRepository(conn).range(
                exchange="NSE", n_days=90,
            )
        assert len(rows) == 6
        # Oldest first → snapshot_dates ascending
        assert all(
            rows[i].snapshot_date <= rows[i + 1].snapshot_date
            for i in range(len(rows) - 1)
        )

    def test_n_days_clips_old_rows(self, db_engine):
        with transaction(db_engine) as conn:
            for offset in (5, 50, 200, 0, 89, 91):
                _seed_row(
                    conn,
                    snapshot_date=date.today() - timedelta(days=offset),
                )
        with db_engine.connect() as conn:
            rows = MarketOverviewRepository(conn).range(
                exchange="NSE", n_days=90,
            )
        # 200 and 91 should be clipped; 5/50/0/89 remain
        offsets_kept = sorted(
            (date.today() - r.snapshot_date).days for r in rows
        )
        assert 200 not in offsets_kept
        assert 91 not in offsets_kept
        assert 89 in offsets_kept

    def test_n_days_zero_returns_empty(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_row(conn, snapshot_date=date.today())
        with db_engine.connect() as conn:
            rows = MarketOverviewRepository(conn).range(
                exchange="NSE", n_days=0,
            )
        assert rows == []

    def test_exchange_scope(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_row(
                conn, exchange="NSE",
                snapshot_date=date.today(),
            )
            _seed_row(
                conn, exchange="BSE",
                snapshot_date=date.today(),
            )
        with db_engine.connect() as conn:
            nse = MarketOverviewRepository(conn).range(
                exchange="NSE", n_days=90,
            )
        assert all(r.exchange == "NSE" for r in nse)


class TestGet:

    def test_get_returns_row_when_present(self, db_engine):
        d = date(2026, 6, 28)
        with transaction(db_engine) as conn:
            _seed_row(conn, snapshot_date=d, n_buys=42)
        with db_engine.connect() as conn:
            row = MarketOverviewRepository(conn).get(
                exchange="NSE", snapshot_date=d,
            )
        assert row is not None
        assert row.n_buys == 42

    def test_get_returns_none_when_missing(self, db_engine):
        with db_engine.connect() as conn:
            row = MarketOverviewRepository(conn).get(
                exchange="NSE",
                snapshot_date=date(1999, 1, 1),
            )
        assert row is None


class TestRowDataclass:

    def test_dataclass_preserves_all_11_counts(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_row(
                conn,
                snapshot_date=date(2026, 6, 28),
                n_buys=1, n_sells=2,
                n_exit_buys=3, n_exit_sells=4,
                n_green_regime=5, n_red_regime=6,
                n_neutral_regime=7,
                n_above_highest_ema=8, n_below_lowest_ema=9,
                n_between_emas=10,
            )
        with db_engine.connect() as conn:
            row = MarketOverviewRepository(conn).latest(
                exchange="NSE",
            )
        assert (
            row.n_buys, row.n_sells,
            row.n_exit_buys, row.n_exit_sells,
            row.n_green_regime, row.n_red_regime,
            row.n_neutral_regime,
            row.n_above_highest_ema, row.n_below_lowest_ema,
            row.n_between_emas,
        ) == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
