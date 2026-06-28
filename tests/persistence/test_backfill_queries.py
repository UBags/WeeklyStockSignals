"""Tests for trip_ema.persistence.backfill_queries (Step 13c).

Five read-side queries powering the ETL Status GUI's Backfill
panel. All tested against the real DB via the db_engine fixture.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import text

from trip_ema.persistence import transaction
from trip_ema.persistence.backfill_queries import (
    _from_dt,
    _monday_of,
    active_symbols,
    historical_signals_delete_count_for_range,
    market_overview_stale_count_for_range,
    min_bar_date,
    resolve_ticker_ids_by_symbol,
)


# ============================================================
# Seed helpers
# ============================================================

def _seed_ticker(
    conn,
    *,
    symbol: str,
    exchange: str = "NSE",
    is_active: bool = True,
    delisted_at=None,
    instrument: str | None = None,
) -> int:
    """Insert a ticker and return ticker_id. `instrument` defaults
    to the symbol — the column is the canonical-identity column
    used in the UNIQUE constraint."""
    inst = instrument or symbol
    row = conn.execute(text("""
        INSERT INTO tickers (
            symbol, instrument, exchange, instrument_type,
            is_active, delisted_at
        ) VALUES (
            :sym, :inst, :ex, 'EQ', :active, :delisted_at
        )
        RETURNING ticker_id
    """), {
        "sym": symbol, "inst": inst, "ex": exchange,
        "active": is_active, "delisted_at": delisted_at,
    }).first()
    return int(row.ticker_id)


def _seed_bar(
    conn,
    *,
    ticker_id: int,
    bar_timestamp: datetime,
    timeframe: str = "1D",
    close: float = 100.0,
) -> None:
    conn.execute(text("""
        INSERT INTO ohlcv_bars (
            ticker_id, timeframe, bar_timestamp,
            open, high, low, close, volume
        ) VALUES (
            :tid, :tf, :ts, :px, :px, :px, :px, 1000
        )
    """), {
        "tid": ticker_id, "tf": timeframe,
        "ts": bar_timestamp, "px": close,
    })


def _seed_historical_signal(
    conn,
    *,
    ticker_id: int,
    bar_timestamp: datetime,
    computed_at: datetime,
    timeframe: str = "1W",
    signal_type: str = "BUY",
    price: float = 100.0,
    regime_before: int = 0,
    regime_after: int = 1,
) -> None:
    conn.execute(text("""
        INSERT INTO historical_signals (
            ticker_id, timeframe, bar_timestamp, signal_type,
            regime_before, regime_after, price_at_signal,
            computed_at
        ) VALUES (
            :tid, :tf, :bts, :st, :rb, :ra, :px, :ca
        )
    """), {
        "tid": ticker_id, "tf": timeframe, "bts": bar_timestamp,
        "st": signal_type, "rb": regime_before, "ra": regime_after,
        "px": price, "ca": computed_at,
    })


def _seed_market_overview_row(
    conn,
    *,
    snapshot_date: date,
    exchange: str = "NSE",
) -> None:
    conn.execute(text("""
        INSERT INTO market_overview_daily (
            snapshot_date, exchange,
            n_buys, n_sells, n_exit_buys, n_exit_sells,
            n_green_regime, n_red_regime, n_neutral_regime,
            n_above_highest_ema, n_below_lowest_ema, n_between_emas
        ) VALUES (
            :d, :ex, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
        )
    """), {"d": snapshot_date, "ex": exchange})


# ============================================================
# Pure helpers
# ============================================================

class TestMondayOf:

    def test_monday_returns_self(self):
        # 2026-06-29 is a Monday
        assert _monday_of(date(2026, 6, 29)) == date(2026, 6, 29)

    def test_friday_returns_preceding_monday(self):
        # 2026-06-26 is a Friday
        assert _monday_of(date(2026, 6, 26)) == date(2026, 6, 22)

    def test_sunday_returns_preceding_monday(self):
        # 2026-06-28 is a Sunday
        assert _monday_of(date(2026, 6, 28)) == date(2026, 6, 22)


class TestFromDt:

    def test_is_utc_anchored(self):
        result = _from_dt(date(2026, 6, 28))
        assert result.tzinfo == UTC

    def test_is_midnight_of_monday(self):
        result = _from_dt(date(2026, 6, 28))
        # Sunday → preceding Monday is 2026-06-22
        assert result == datetime(
            2026, 6, 22, 0, 0, tzinfo=UTC,
        )


# ============================================================
# active_symbols
# ============================================================

class TestActiveSymbols:

    def test_empty(self, db_engine):
        with db_engine.connect() as conn:
            assert active_symbols(conn, exchange="NSE") == []

    def test_returns_alphabetical(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, symbol="TCS")
            _seed_ticker(conn, symbol="ACC")
            _seed_ticker(conn, symbol="REL")
        with db_engine.connect() as conn:
            syms = active_symbols(conn, exchange="NSE")
        assert syms == ["ACC", "REL", "TCS"]

    def test_excludes_inactive(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, symbol="ACTIVE")
            _seed_ticker(conn, symbol="INACTIVE", is_active=False)
        with db_engine.connect() as conn:
            assert active_symbols(conn, exchange="NSE") == ["ACTIVE"]

    def test_excludes_delisted(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, symbol="LIVE")
            _seed_ticker(
                conn, symbol="DEAD",
                delisted_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
        with db_engine.connect() as conn:
            assert active_symbols(conn, exchange="NSE") == ["LIVE"]

    def test_filter_by_exchange(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, symbol="NSE_A", exchange="NSE")
            _seed_ticker(
                conn, symbol="BSE_A", exchange="BSE",
                instrument="BSE_A_BSE",
            )
        with db_engine.connect() as conn:
            assert active_symbols(conn, exchange="NSE") == ["NSE_A"]
            assert active_symbols(conn, exchange="BSE") == ["BSE_A"]


# ============================================================
# min_bar_date
# ============================================================

class TestMinBarDate:

    def test_none_when_no_bars(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, symbol="A")
        with db_engine.connect() as conn:
            assert min_bar_date(conn, exchange="NSE") is None

    def test_returns_earliest_daily_bar(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn, symbol="A")
            _seed_bar(
                conn, ticker_id=tid,
                bar_timestamp=datetime(2020, 5, 1, tzinfo=UTC),
            )
            _seed_bar(
                conn, ticker_id=tid,
                bar_timestamp=datetime(2018, 3, 15, tzinfo=UTC),
            )
            _seed_bar(
                conn, ticker_id=tid,
                bar_timestamp=datetime(2022, 9, 1, tzinfo=UTC),
            )
        with db_engine.connect() as conn:
            assert min_bar_date(conn, exchange="NSE") == date(
                2018, 3, 15,
            )

    def test_filters_to_daily_timeframe(self, db_engine):
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn, symbol="A")
            # Weekly bar earlier than daily — should be ignored
            _seed_bar(
                conn, ticker_id=tid,
                bar_timestamp=datetime(2015, 1, 1, tzinfo=UTC),
                timeframe="1W",
            )
            _seed_bar(
                conn, ticker_id=tid,
                bar_timestamp=datetime(2020, 1, 1, tzinfo=UTC),
                timeframe="1D",
            )
        with db_engine.connect() as conn:
            # min returns the daily, ignoring the weekly
            assert min_bar_date(conn, exchange="NSE") == date(
                2020, 1, 1,
            )

    def test_filter_by_exchange(self, db_engine):
        with transaction(db_engine) as conn:
            tid_nse = _seed_ticker(
                conn, symbol="A", exchange="NSE",
            )
            tid_bse = _seed_ticker(
                conn, symbol="B", exchange="BSE",
                instrument="B_BSE",
            )
            _seed_bar(
                conn, ticker_id=tid_nse,
                bar_timestamp=datetime(2020, 1, 1, tzinfo=UTC),
            )
            _seed_bar(
                conn, ticker_id=tid_bse,
                bar_timestamp=datetime(2010, 1, 1, tzinfo=UTC),
            )
        with db_engine.connect() as conn:
            assert min_bar_date(conn, exchange="NSE") == date(
                2020, 1, 1,
            )
            assert min_bar_date(conn, exchange="BSE") == date(
                2010, 1, 1,
            )


# ============================================================
# historical_signals_delete_count_for_range
# ============================================================

class TestHistoricalDeleteCount:

    def test_zero_when_no_signals(self, db_engine):
        with db_engine.connect() as conn:
            n = historical_signals_delete_count_for_range(
                conn, exchange="NSE",
                date_from=date(2024, 1, 1),
            )
        assert n == 0

    def test_zero_when_empty_ticker_ids_list(self, db_engine):
        with db_engine.connect() as conn:
            n = historical_signals_delete_count_for_range(
                conn, exchange="NSE",
                date_from=date(2024, 1, 1),
                ticker_ids=[],
            )
        assert n == 0

    def test_counts_by_computed_at_one_sided(self, db_engine):
        # Mirrors the pipeline's DELETE — computed_at >= from_dt
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn, symbol="A")
            # All bars on same week; difference is in computed_at
            bts = datetime(2024, 6, 17, tzinfo=UTC)  # a Monday
            _seed_historical_signal(
                conn, ticker_id=tid, bar_timestamp=bts,
                computed_at=datetime(2023, 12, 31, tzinfo=UTC),
            )
            _seed_historical_signal(
                conn, ticker_id=tid, bar_timestamp=bts,
                computed_at=datetime(2024, 1, 1, tzinfo=UTC),
            )
            _seed_historical_signal(
                conn, ticker_id=tid, bar_timestamp=bts,
                computed_at=datetime(2024, 6, 1, tzinfo=UTC),
            )
        # 2024-01-01 IS a Monday → _monday_of() is a no-op,
        with db_engine.connect() as conn:
            n = historical_signals_delete_count_for_range(
                conn, exchange="NSE",
                date_from=date(2024, 1, 1),
                ticker_ids=[tid],
            )
        # from_dt = 2024-01-01 00:00 UTC.
        # Excludes the 2023-12-31 seed; counts the other two.
        assert n == 2

    def test_by_explicit_ticker_ids(self, db_engine):
        with transaction(db_engine) as conn:
            tid_a = _seed_ticker(conn, symbol="A")
            tid_b = _seed_ticker(conn, symbol="B")
            bts = datetime(2024, 6, 17, tzinfo=UTC)
            ca = datetime(2024, 6, 17, tzinfo=UTC)
            _seed_historical_signal(
                conn, ticker_id=tid_a,
                bar_timestamp=bts, computed_at=ca,
            )
            _seed_historical_signal(
                conn, ticker_id=tid_b,
                bar_timestamp=bts, computed_at=ca,
            )
        with db_engine.connect() as conn:
            assert (
                historical_signals_delete_count_for_range(
                    conn, exchange="NSE",
                    date_from=date(2024, 1, 1),
                    ticker_ids=[tid_a],
                ) == 1
            )

    def test_all_active_scope_when_ticker_ids_none(self, db_engine):
        with transaction(db_engine) as conn:
            tid_active = _seed_ticker(conn, symbol="A")
            tid_inactive = _seed_ticker(
                conn, symbol="B", is_active=False,
            )
            bts = datetime(2024, 6, 17, tzinfo=UTC)
            ca = datetime(2024, 6, 17, tzinfo=UTC)
            _seed_historical_signal(
                conn, ticker_id=tid_active,
                bar_timestamp=bts, computed_at=ca,
            )
            _seed_historical_signal(
                conn, ticker_id=tid_inactive,
                bar_timestamp=bts, computed_at=ca,
            )
        with db_engine.connect() as conn:
            # ticker_ids=None → scope is active tickers only
            n = historical_signals_delete_count_for_range(
                conn, exchange="NSE",
                date_from=date(2024, 1, 1),
                ticker_ids=None,
            )
        assert n == 1


# ============================================================
# market_overview_stale_count_for_range
# ============================================================

class TestMarketOverviewStaleCount:

    def test_zero_when_empty(self, db_engine):
        with db_engine.connect() as conn:
            n = market_overview_stale_count_for_range(
                conn, exchange="NSE",
                date_from=date(2024, 1, 1),
            )
        assert n == 0

    def test_one_sided_cutoff(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_market_overview_row(
                conn, snapshot_date=date(2023, 12, 31),
            )
            _seed_market_overview_row(
                conn, snapshot_date=date(2024, 1, 1),
            )
            _seed_market_overview_row(
                conn, snapshot_date=date(2025, 6, 1),
            )
        with db_engine.connect() as conn:
            n = market_overview_stale_count_for_range(
                conn, exchange="NSE",
                date_from=date(2024, 1, 1),
            )
        # 2024-01-01 + 2025-06-01 (the >= condition)
        assert n == 2

    def test_filter_by_exchange(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_market_overview_row(
                conn, snapshot_date=date(2024, 1, 1),
                exchange="NSE",
            )
            _seed_market_overview_row(
                conn, snapshot_date=date(2024, 1, 1),
                exchange="BSE",
            )
        with db_engine.connect() as conn:
            n_nse = market_overview_stale_count_for_range(
                conn, exchange="NSE",
                date_from=date(2024, 1, 1),
            )
            n_bse = market_overview_stale_count_for_range(
                conn, exchange="BSE",
                date_from=date(2024, 1, 1),
            )
        assert n_nse == 1
        assert n_bse == 1


# ============================================================
# resolve_ticker_ids_by_symbol
# ============================================================

class TestResolveTickerIds:

    def test_empty_input(self, db_engine):
        with db_engine.connect() as conn:
            resolved, unresolved = resolve_ticker_ids_by_symbol(
                conn, exchange="NSE", symbols=[],
            )
        assert resolved == []
        assert unresolved == []

    def test_full_resolution(self, db_engine):
        with transaction(db_engine) as conn:
            tid_a = _seed_ticker(conn, symbol="ACC")
            tid_b = _seed_ticker(conn, symbol="REL")
        with db_engine.connect() as conn:
            resolved, unresolved = resolve_ticker_ids_by_symbol(
                conn, exchange="NSE", symbols=["ACC", "REL"],
            )
        assert resolved == sorted([tid_a, tid_b])
        assert unresolved == []

    def test_normalizes_case(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, symbol="ACC")
        with db_engine.connect() as conn:
            resolved, unresolved = resolve_ticker_ids_by_symbol(
                conn, exchange="NSE", symbols=["acc"],
            )
        assert len(resolved) == 1
        assert unresolved == []

    def test_unresolved_returned(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, symbol="ACC")
        with db_engine.connect() as conn:
            resolved, unresolved = resolve_ticker_ids_by_symbol(
                conn, exchange="NSE",
                symbols=["ACC", "GHOST", "PHANTOM"],
            )
        assert len(resolved) == 1
        assert sorted(unresolved) == ["GHOST", "PHANTOM"]

    def test_skips_inactive(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, symbol="ACTIVE")
            _seed_ticker(
                conn, symbol="INACTIVE", is_active=False,
            )
        with db_engine.connect() as conn:
            resolved, unresolved = resolve_ticker_ids_by_symbol(
                conn, exchange="NSE",
                symbols=["ACTIVE", "INACTIVE"],
            )
        assert len(resolved) == 1
        assert unresolved == ["INACTIVE"]
