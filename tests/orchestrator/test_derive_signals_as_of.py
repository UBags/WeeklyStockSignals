"""Tests for the `as_of` extension to derive_signal_for_ticker_this_cycle.

Step 11b1 added an `as_of: datetime | None = None` parameter to
derive_signal_for_ticker_this_cycle. When set, it bounds the
state-row read AND caps the daily-close price lookup. None preserves
the live cycle's behavior (latest 2 rows, absolute latest price).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from trip_ema.orchestrator.derive_signals import (
    derive_signal_for_ticker_this_cycle,
)
from trip_ema.persistence import transaction


_IST = ZoneInfo("Asia/Kolkata")


def _seed_ticker(db_engine, ticker_id: int = 1) -> None:
    with transaction(db_engine) as conn:
        conn.execute(text("""
            INSERT INTO tickers (
                ticker_id, exchange, symbol, tier,
                is_active, delisted_at
            ) VALUES (:tid, 'NSE', 'TEST', 2, TRUE, NULL)
            ON CONFLICT (ticker_id) DO NOTHING
        """), {"tid": ticker_id})


def _seed_state(
    db_engine, *, ticker_id: int,
    rows: list[tuple[date, int]],
) -> None:
    """rows: list of (monday_date, display_regime)."""
    with transaction(db_engine) as conn:
        for monday, regime in rows:
            bar_ts = datetime.combine(monday, time(0, 0), tzinfo=UTC)
            conn.execute(text("""
                INSERT INTO stock_indicator_state (
                    ticker_id, timeframe, date, bar_timestamp,
                    med_ema, long_ema, longest_ema, atr,
                    upper_inner_band, lower_inner_band,
                    upper_mid_band, lower_mid_band,
                    upper_outer_band, lower_outer_band,
                    display_regime, computed_at
                ) VALUES (
                    :tid, '1W', :dt, :ts,
                    100.0, 99.0, 98.0, 2.0,
                    102.0, 96.0, 104.0, 94.0, 106.0, 92.0,
                    :reg, NOW()
                )
                ON CONFLICT (ticker_id, timeframe, date)
                DO UPDATE SET display_regime = EXCLUDED.display_regime
            """), {"tid": ticker_id, "ts": bar_ts, "reg": regime,
                   "dt": monday})


def _seed_daily(
    db_engine, *, ticker_id: int,
    closes: list[tuple[date, float]],
) -> None:
    with transaction(db_engine) as conn:
        for d, c in closes:
            bar_ts = datetime.combine(d, time(0, 0), tzinfo=UTC)
            conn.execute(text("""
                INSERT INTO ohlcv_bars (
                    ticker_id, timeframe, bar_timestamp,
                    open, high, low, close, volume
                ) VALUES (:tid, '1D', :ts, :c, :c, :c, :c, 1000)
                ON CONFLICT (ticker_id, timeframe, bar_timestamp)
                DO UPDATE SET close = EXCLUDED.close
            """), {"tid": ticker_id, "ts": bar_ts, "c": c})


class TestAsOfParameter:

    def test_default_none_preserves_live_behavior(self, db_engine):
        """When as_of is None, the function reads the latest 2 state
        rows and the absolute latest daily close — the pre-Step-11b1
        live cycle behavior."""
        _seed_ticker(db_engine)
        _seed_state(db_engine, ticker_id=1, rows=[
            (date(2024, 6, 3), 1),
            (date(2024, 6, 10), -1),  # latest is bearish
        ])
        _seed_daily(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 10), 100.0),
        ])
        with db_engine.connect() as conn:
            ev = derive_signal_for_ticker_this_cycle(
                conn, 1, now=datetime.now(UTC),
            )
        assert ev is not None
        assert ev.regime_before == 1
        assert ev.regime_after == -1
        assert ev.price_at_signal == 100.0

    def test_as_of_bounds_state_read(self, db_engine):
        """With as_of set, the function reads the latest 2 state
        rows where bar_timestamp <= as_of. Future rows are invisible."""
        _seed_ticker(db_engine)
        _seed_state(db_engine, ticker_id=1, rows=[
            (date(2024, 6, 3), 1),
            (date(2024, 6, 10), -1),    # flip at 6/10
            (date(2024, 6, 17), 1),     # un-flip at 6/17
        ])
        _seed_daily(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 10), 100.0),
        ])
        # as_of = 6/10 → should see 6/3 vs 6/10 → flip detected
        as_of = datetime.combine(
            date(2024, 6, 10), time(16, 30), tzinfo=_IST,
        )
        with db_engine.connect() as conn:
            ev = derive_signal_for_ticker_this_cycle(
                conn, 1, now=as_of, as_of=as_of,
            )
        assert ev is not None
        assert ev.regime_before == 1
        assert ev.regime_after == -1

    def test_as_of_caps_price_lookup(self, db_engine):
        """When on_or_before is None but as_of is set, the price
        lookup uses as_of as the cap. Otherwise live cycle would
        attribute a future price to a historical signal."""
        _seed_ticker(db_engine)
        _seed_state(db_engine, ticker_id=1, rows=[
            (date(2024, 6, 3), 1),
            (date(2024, 6, 10), -1),
        ])
        _seed_daily(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 10), 100.0),
            (date(2024, 6, 17), 200.0),   # future price
        ])
        # as_of = 6/10 → should pick 100.0, NOT the future 200.0
        as_of = datetime.combine(
            date(2024, 6, 10), time(16, 30), tzinfo=_IST,
        )
        with db_engine.connect() as conn:
            ev = derive_signal_for_ticker_this_cycle(
                conn, 1, now=as_of, as_of=as_of,
            )
        assert ev is not None
        assert ev.price_at_signal == 100.0

    def test_explicit_on_or_before_wins_over_as_of(self, db_engine):
        """If the caller passes BOTH as_of AND on_or_before, the
        explicit on_or_before wins for the price lookup."""
        _seed_ticker(db_engine)
        _seed_state(db_engine, ticker_id=1, rows=[
            (date(2024, 6, 3), 1),
            (date(2024, 6, 10), -1),
        ])
        _seed_daily(db_engine, ticker_id=1, closes=[
            (date(2024, 6, 3), 50.0),   # earlier price
            (date(2024, 6, 10), 100.0),
        ])
        as_of = datetime.combine(
            date(2024, 6, 10), time(16, 30), tzinfo=_IST,
        )
        explicit = datetime.combine(
            date(2024, 6, 3), time(16, 30), tzinfo=_IST,
        )
        with db_engine.connect() as conn:
            ev = derive_signal_for_ticker_this_cycle(
                conn, 1,
                now=as_of, as_of=as_of,
                on_or_before=explicit,
            )
        assert ev is not None
        assert ev.price_at_signal == 50.0
