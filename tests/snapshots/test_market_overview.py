"""Tests for MarketOverviewSnapshotter (Step 13a).

Verifies the 11 aggregate counts are computed correctly for a
controlled universe of seeded tickers + state + daily bars +
current_signals.

The snapshot pulls from THREE sources:
  - stock_indicator_state (regime + EMAs at in-progress weekly bar)
  - ohlcv_bars (daily close for snapshot_date)
  - current_signals (signal_type contributions)

And aggregates to one row in market_overview_daily.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, UTC
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.domain.signal_event import SignalEvent
from trip_ema.persistence import TickersRepository, transaction
from trip_ema.persistence.current_signals_repo import (
    CurrentSignalsRepository,
)
from trip_ema.snapshots.market_overview import (
    MarketOverviewCounts,
    MarketOverviewSnapshotter,
)


IST = ZoneInfo("Asia/Kolkata")


def _ist_today() -> date:
    return datetime.now(IST).date()


def _monday_of_ist_week(d: date) -> date:
    """Postgres date_trunc('week', ...) returns the Monday of the
    week; Python's date.weekday() is 0=Monday."""
    return d - timedelta(days=d.weekday())


def _monday_ts_for(d: date) -> datetime:
    """Monday 00:00 UTC of d's IST week — the bar_timestamp shape
    used by stock_indicator_state for weekly bars."""
    monday = _monday_of_ist_week(d)
    return datetime.combine(monday, time(0, 0), tzinfo=UTC)


@pytest.fixture
def snap_date():
    """Snapshot for today's IST date (matches the function's
    default when snapshot_date=None)."""
    return _ist_today()


@pytest.fixture
def monday_ts(snap_date):
    return _monday_ts_for(snap_date)


def _insert_ticker(
    conn, symbol: str, *, exchange: str = "NSE", now=None,
) -> int:
    return TickersRepository(conn).insert(
        TickerRef(exchange, symbol, "EQ"),
        created_at=now or datetime.now(UTC),
    )


def _seed_state(
    conn, *,
    ticker_id: int, monday_ts: datetime,
    med_ema: float, long_ema: float, longest_ema: float,
    regime: int,
):
    """Insert one stock_indicator_state row at the in-progress
    weekly bar."""
    conn.execute(text("""
        INSERT INTO stock_indicator_state (
            ticker_id, timeframe, date, bar_timestamp,
            med_ema, long_ema, longest_ema, atr,
            upper_inner_band, lower_inner_band,
            upper_mid_band, lower_mid_band,
            upper_outer_band, lower_outer_band,
            display_regime, computed_at
        ) VALUES (
            :tid, '1W', (:ts AT TIME ZONE 'Asia/Kolkata')::date, :ts,
            :med, :long, :longest, 2.0,
            120.0, 80.0, 130.0, 70.0, 140.0, 60.0,
            :reg, NOW()
        )
    """), {
        "tid": ticker_id, "ts": monday_ts,
        "med": med_ema, "long": long_ema, "longest": longest_ema,
        "reg": regime,
    })


def _seed_daily_close(
    conn, *, ticker_id: int, snap_date: date, close: float,
):
    """Insert one ohlcv_bars row at snap_date with the given close."""
    conn.execute(text("""
        INSERT INTO ohlcv_bars (
            ticker_id, timeframe, bar_timestamp,
            open, high, low, close, volume
        ) VALUES (
            :tid, '1D', :ts,
            :c, :c, :c, :c, 1000
        )
    """), {
        "tid": ticker_id,
        "ts": datetime.combine(snap_date, time(0, 0), tzinfo=UTC),
        "c": close,
    })


def _make_signal(
    *, ticker_id: int, bar_ts: datetime, signal_type: str,
) -> SignalEvent:
    regime_map = {
        "BUY":            (-1,  1),
        "SELL":           ( 1, -1),
        "EXIT_FROM_BUY":  ( 1,  0),
        "EXIT_FROM_SELL": (-1,  0),
    }
    rb, ra = regime_map[signal_type]
    return SignalEvent(
        ticker_id=ticker_id,
        timeframe="1W",
        bar_timestamp=bar_ts,
        signal_type=signal_type,
        regime_before=rb,
        regime_after=ra,
        price_at_signal=100.0,
        computed_at=bar_ts,
    )


# ============================================================
# Empty universe
# ============================================================

class TestEmptyUniverse:

    def test_no_tickers_yields_all_zeros(self, db_engine, snap_date):
        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts == MarketOverviewCounts(
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        )

    def test_empty_universe_row_persisted(
        self, db_engine, snap_date,
    ):
        snap = MarketOverviewSnapshotter(db_engine)
        snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT n_buys, n_green_regime, computed_at
                  FROM market_overview_daily
                 WHERE snapshot_date = :d AND exchange = 'NSE'
            """), {"d": snap_date}).first()
        assert row is not None
        assert row.n_buys == 0
        assert row.n_green_regime == 0


# ============================================================
# Signal counts
# ============================================================

class TestSignalCounts:

    def test_signals_grouped_by_type(
        self, db_engine, snap_date, monday_ts,
    ):
        with transaction(db_engine) as conn:
            ids = [
                _insert_ticker(conn, f"T{i}", now=datetime.now(UTC))
                for i in range(4)
            ]
            sigs = [
                _make_signal(
                    ticker_id=ids[0], bar_ts=monday_ts,
                    signal_type="BUY",
                ),
                _make_signal(
                    ticker_id=ids[1], bar_ts=monday_ts,
                    signal_type="SELL",
                ),
                _make_signal(
                    ticker_id=ids[2], bar_ts=monday_ts,
                    signal_type="EXIT_FROM_BUY",
                ),
                _make_signal(
                    ticker_id=ids[3], bar_ts=monday_ts,
                    signal_type="EXIT_FROM_SELL",
                ),
            ]
            CurrentSignalsRepository(conn).insert_many(sigs)

        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts.n_buys == 1
        assert counts.n_sells == 1
        assert counts.n_exit_buys == 1
        assert counts.n_exit_sells == 1
        assert counts.total_signals == 4

    def test_signal_at_old_bar_NOT_counted(
        self, db_engine, snap_date, monday_ts,
    ):
        """A current_signals row whose bar_timestamp doesn't match
        the in-progress weekly Monday should NOT contribute."""
        old_monday = monday_ts - timedelta(days=7)
        with transaction(db_engine) as conn:
            tid = _insert_ticker(conn, "OLD")
            CurrentSignalsRepository(conn).insert_many([
                _make_signal(
                    ticker_id=tid, bar_ts=old_monday,
                    signal_type="BUY",
                ),
            ])

        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts.n_buys == 0


# ============================================================
# Regime counts
# ============================================================

class TestRegimeCounts:

    def test_regimes_counted_at_in_progress_bar(
        self, db_engine, snap_date, monday_ts,
    ):
        with transaction(db_engine) as conn:
            green_id = _insert_ticker(conn, "GREEN")
            red_id = _insert_ticker(conn, "RED")
            neutral_id = _insert_ticker(conn, "NEUTRAL")
            for tid, regime in (
                (green_id, 1),
                (red_id, -1),
                (neutral_id, 0),
            ):
                _seed_state(
                    conn, ticker_id=tid, monday_ts=monday_ts,
                    med_ema=100.0, long_ema=99.0, longest_ema=98.0,
                    regime=regime,
                )

        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts.n_green_regime == 1
        assert counts.n_red_regime == 1
        assert counts.n_neutral_regime == 1
        assert counts.total_regimes == 3


# ============================================================
# EMA envelope counts
# ============================================================

class TestEMAEnvelope:

    def test_close_above_highest_ema(
        self, db_engine, snap_date, monday_ts,
    ):
        """med=100, long=105, longest=110 → highest_ema=110.
        close=120 → above_highest."""
        with transaction(db_engine) as conn:
            tid = _insert_ticker(conn, "ABV")
            _seed_state(
                conn, ticker_id=tid, monday_ts=monday_ts,
                med_ema=100.0, long_ema=105.0, longest_ema=110.0,
                regime=1,
            )
            _seed_daily_close(
                conn, ticker_id=tid, snap_date=snap_date,
                close=120.0,
            )

        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts.n_above_highest_ema == 1
        assert counts.n_below_lowest_ema == 0
        assert counts.n_between_emas == 0

    def test_close_below_lowest_ema(
        self, db_engine, snap_date, monday_ts,
    ):
        """med=100, long=105, longest=110 → lowest_ema=100.
        close=90 → below_lowest."""
        with transaction(db_engine) as conn:
            tid = _insert_ticker(conn, "BLW")
            _seed_state(
                conn, ticker_id=tid, monday_ts=monday_ts,
                med_ema=100.0, long_ema=105.0, longest_ema=110.0,
                regime=-1,
            )
            _seed_daily_close(
                conn, ticker_id=tid, snap_date=snap_date,
                close=90.0,
            )

        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts.n_below_lowest_ema == 1
        assert counts.n_above_highest_ema == 0
        assert counts.n_between_emas == 0

    def test_close_between_emas(
        self, db_engine, snap_date, monday_ts,
    ):
        """med=100, long=105, longest=110.
        close=104 → strictly between [100, 110]."""
        with transaction(db_engine) as conn:
            tid = _insert_ticker(conn, "MID")
            _seed_state(
                conn, ticker_id=tid, monday_ts=monday_ts,
                med_ema=100.0, long_ema=105.0, longest_ema=110.0,
                regime=0,
            )
            _seed_daily_close(
                conn, ticker_id=tid, snap_date=snap_date,
                close=104.0,
            )

        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts.n_between_emas == 1
        assert counts.n_above_highest_ema == 0
        assert counts.n_below_lowest_ema == 0

    def test_close_exactly_at_highest_counts_as_above(
        self, db_engine, snap_date, monday_ts,
    ):
        """close >= highest_ema → above (>= boundary)."""
        with transaction(db_engine) as conn:
            tid = _insert_ticker(conn, "EQH")
            _seed_state(
                conn, ticker_id=tid, monday_ts=monday_ts,
                med_ema=100.0, long_ema=105.0, longest_ema=110.0,
                regime=1,
            )
            _seed_daily_close(
                conn, ticker_id=tid, snap_date=snap_date,
                close=110.0,
            )

        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts.n_above_highest_ema == 1

    def test_missing_daily_close_excluded(
        self, db_engine, snap_date, monday_ts,
    ):
        """If no daily bar exists for the snap date, the ticker
        contributes neither above nor below nor between."""
        with transaction(db_engine) as conn:
            tid = _insert_ticker(conn, "NOCLS")
            _seed_state(
                conn, ticker_id=tid, monday_ts=monday_ts,
                med_ema=100.0, long_ema=105.0, longest_ema=110.0,
                regime=1,
            )
            # No daily close seeded.

        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts.n_above_highest_ema == 0
        assert counts.n_below_lowest_ema == 0
        assert counts.n_between_emas == 0


# ============================================================
# Exchange filter + UPSERT
# ============================================================

class TestExchangeAndUpsert:

    def test_exchange_filter_excludes_others(
        self, db_engine, snap_date, monday_ts,
    ):
        with transaction(db_engine) as conn:
            nse_tid = _insert_ticker(
                conn, "N", exchange="NSE",
            )
            bse_tid = _insert_ticker(
                conn, "B", exchange="BSE",
            )
            for tid in (nse_tid, bse_tid):
                _seed_state(
                    conn, ticker_id=tid, monday_ts=monday_ts,
                    med_ema=100.0, long_ema=99.0, longest_ema=98.0,
                    regime=1,
                )

        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts.n_green_regime == 1  # only NSE

    def test_upsert_overwrites_prior_snapshot(
        self, db_engine, snap_date, monday_ts,
    ):
        """Run snapshot twice for the same (date, exchange) —
        second run's counts should win."""
        with transaction(db_engine) as conn:
            tid = _insert_ticker(conn, "T")
            _seed_state(
                conn, ticker_id=tid, monday_ts=monday_ts,
                med_ema=100.0, long_ema=99.0, longest_ema=98.0,
                regime=1,
            )

        snap = MarketOverviewSnapshotter(db_engine)
        snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        # Add a second ticker, re-run
        with transaction(db_engine) as conn:
            tid2 = _insert_ticker(conn, "T2")
            _seed_state(
                conn, ticker_id=tid2, monday_ts=monday_ts,
                med_ema=100.0, long_ema=99.0, longest_ema=98.0,
                regime=1,
            )
        snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )

        with db_engine.connect() as conn:
            count = conn.execute(text("""
                SELECT n_green_regime FROM market_overview_daily
                 WHERE snapshot_date = :d AND exchange = 'NSE'
            """), {"d": snap_date}).scalar_one()
        assert count == 2  # latest snapshot wins

    def test_inactive_ticker_excluded(
        self, db_engine, snap_date, monday_ts,
    ):
        with transaction(db_engine) as conn:
            tid = _insert_ticker(conn, "INA")
            conn.execute(text("""
                UPDATE tickers SET is_active = FALSE
                 WHERE ticker_id = :tid
            """), {"tid": tid})
            _seed_state(
                conn, ticker_id=tid, monday_ts=monday_ts,
                med_ema=100.0, long_ema=99.0, longest_ema=98.0,
                regime=1,
            )

        snap = MarketOverviewSnapshotter(db_engine)
        counts = snap.snapshot_for_today(
            exchange="NSE", snapshot_date=snap_date,
        )
        assert counts.n_green_regime == 0


# ============================================================
# delete_range
# ============================================================

class TestDeleteRange:

    def test_delete_range_removes_matching_rows(
        self, db_engine, snap_date,
    ):
        snap = MarketOverviewSnapshotter(db_engine)
        # Seed 3 days
        for offset in range(3):
            d = snap_date - timedelta(days=offset)
            snap.snapshot_for_today(
                exchange="NSE", snapshot_date=d,
            )

        deleted = snap.delete_range(
            exchange="NSE",
            date_from=snap_date - timedelta(days=2),
            date_to=snap_date,
        )
        assert deleted == 3

        with db_engine.connect() as conn:
            remaining = conn.execute(text("""
                SELECT COUNT(*) FROM market_overview_daily
                 WHERE exchange = 'NSE'
            """)).scalar_one()
        assert remaining == 0
