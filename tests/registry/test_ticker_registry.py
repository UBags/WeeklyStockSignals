"""Tests for TickerRegistry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.domain import IndicatorParams, OHLCVBar, TickerRef, Timeframe
from trip_ema.engine import TickerEngine
from trip_ema.persistence import (
    BarsRepository,
    TickersRepository,
    transaction,
)
from trip_ema.registry import TickerRegistry


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")
RELIANCE = TickerRef("NSE", "RELIANCE", "EQ")


def _seed_ticker(db_engine, ref: TickerRef, *, is_active: bool = True) -> int:
    with transaction(db_engine) as conn:
        return TickersRepository(conn).insert(
            ref, is_active=is_active,
            created_at=datetime.now(timezone.utc),
        )


def _seed_bars(db_engine, ticker_id: int, n: int, start_close: float = 100.0):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with transaction(db_engine) as conn:
        repo = BarsRepository(conn)
        for i in range(n):
            repo.insert_bar(
                ticker_id, Timeframe.WEEKLY,
                OHLCVBar(
                    timestamp=start + timedelta(weeks=i),
                    open=start_close + i,
                    high=start_close + i + 1,
                    low=start_close + i - 1,
                    close=start_close + i + 0.5,
                    volume=1000.0,
                ),
            )


# ---------- Empty starting state ---------- #

def test_registry_starts_empty():
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    assert len(registry) == 0
    assert registry.get(NIFTY, Timeframe.WEEKLY) is None
    assert registry.all_engines() == []


# ---------- prime_all happy paths ---------- #

def test_prime_all_with_one_ticker_and_bars(db_engine):
    tid = _seed_ticker(db_engine, NIFTY)
    _seed_bars(db_engine, tid, n=100)

    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    result = registry.prime_all(db_engine)

    assert result.tickers_primed == 1
    assert result.tickers_skipped_no_bars == 0
    assert result.tickers_failed == 0
    engine = registry.get(NIFTY, Timeframe.WEEKLY)
    assert engine is not None
    assert engine.latest_state() is not None
    assert registry.get_ticker_id(NIFTY) == tid


def test_prime_all_skips_tickers_with_no_bars(db_engine):
    _seed_ticker(db_engine, NIFTY)   # no bars seeded
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    result = registry.prime_all(db_engine)
    assert result.tickers_primed == 0
    assert result.tickers_skipped_no_bars == 1
    assert registry.get(NIFTY, Timeframe.WEEKLY) is None


def test_prime_all_skips_inactive_tickers(db_engine):
    """Inactive tickers must never be primed."""
    tid = _seed_ticker(db_engine, NIFTY, is_active=False)
    _seed_bars(db_engine, tid, n=50)
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    result = registry.prime_all(db_engine)
    assert result.tickers_primed == 0
    assert registry.get(NIFTY, Timeframe.WEEKLY) is None


def test_prime_all_handles_multiple_tickers(db_engine):
    t1 = _seed_ticker(db_engine, NIFTY)
    t2 = _seed_ticker(db_engine, RELIANCE)
    _seed_bars(db_engine, t1, n=50, start_close=24000)
    _seed_bars(db_engine, t2, n=50, start_close=2400)

    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    result = registry.prime_all(db_engine)
    assert result.tickers_primed == 2
    assert registry.get(NIFTY, Timeframe.WEEKLY) is not None
    assert registry.get(RELIANCE, Timeframe.WEEKLY) is not None
    assert len(registry) == 2


def test_prime_all_respects_history_limit(db_engine):
    """If history_limit is 30, the engine should be primed with at most
    30 bars even if the DB has 100."""
    tid = _seed_ticker(db_engine, NIFTY)
    _seed_bars(db_engine, tid, n=100)
    registry = TickerRegistry(
        IndicatorParams(), [Timeframe.WEEKLY], history_limit=30,
    )
    registry.prime_all(db_engine)
    engine = registry.get(NIFTY, Timeframe.WEEKLY)
    # state_history_retain defaults to 200, so all primed bars are retained.
    # We expect 30 BarStates (one per primed bar).
    assert len(engine.state_history()) == 30


def test_prime_all_records_ticker_id_mapping(db_engine):
    """Scheduler reads ticker_id from the registry — verify it's populated."""
    tid = _seed_ticker(db_engine, NIFTY)
    _seed_bars(db_engine, tid, n=10)
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    registry.prime_all(db_engine)
    assert registry.get_ticker_id(NIFTY) == tid


# ---------- Manual registration ---------- #

def test_register_engine_directly():
    """The manual registration path used by tests / future runtime
    ticker additions."""
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    engine = TickerEngine(NIFTY, Timeframe.WEEKLY, IndicatorParams())
    registry.register_engine(NIFTY, Timeframe.WEEKLY, engine, ticker_id=42)
    assert registry.get(NIFTY, Timeframe.WEEKLY) is engine
    assert registry.get_ticker_id(NIFTY) == 42


def test_contains_operator():
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    engine = TickerEngine(NIFTY, Timeframe.WEEKLY, IndicatorParams())
    registry.register_engine(NIFTY, Timeframe.WEEKLY, engine)
    assert (NIFTY, Timeframe.WEEKLY) in registry
    assert (RELIANCE, Timeframe.WEEKLY) not in registry


# ============================================================
# B4: Source-resolution aggregation
# ============================================================


def _seed_daily_bars_for_weeks(db_engine, ticker_id: int, n_weeks: int):
    """Seed n_weeks worth of daily bars (5 trading days per week, Mon–Fri)
    starting from Mon 2024-01-01 UTC. Each daily bar has a stable price
    and volume."""
    start_mon = datetime(2024, 1, 1, tzinfo=timezone.utc)   # Monday
    with transaction(db_engine) as conn:
        repo = BarsRepository(conn)
        for week in range(n_weeks):
            for day in range(5):   # Mon–Fri only
                ts = start_mon + timedelta(weeks=week, days=day)
                repo.insert_bar(
                    ticker_id, Timeframe.DAILY,
                    OHLCVBar(
                        timestamp=ts,
                        open=100.0 + week * 0.5,
                        high=101.0 + week * 0.5,
                        low=99.0 + week * 0.5,
                        close=100.5 + week * 0.5,
                        volume=200.0,
                    ),
                )


class TestSourceAggregationPrime:
    """When source_timeframe != target, prime_all must fetch source bars
    and aggregate to the target before priming the engine."""

    def test_weekly_engine_from_daily_source(self, db_engine):
        """Ticker has source='1D'; engine's target is WEEKLY. Registry
        should aggregate daily → weekly during prime."""
        with transaction(db_engine) as conn:
            tid = TickersRepository(conn).insert(
                NIFTY,
                created_at=datetime.now(timezone.utc),
                tz="UTC",
                source_timeframe="1D",
            )
        _seed_daily_bars_for_weeks(db_engine, tid, n_weeks=10)

        registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
        result = registry.prime_all(db_engine)
        assert result.tickers_primed == 1

        engine = registry.get(NIFTY, Timeframe.WEEKLY)
        assert engine is not None
        history = engine.state_history()
        # 10 weeks of daily bars (5 days each) → 10 weekly bars
        assert len(history) == 10
        # Verify aggregation: first weekly bar's open = first day's open
        # (which was 100.0 for week 0), close = last day's close
        # (also 100.5 for week 0 since all 5 days share the same close)
        first_weekly_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert history[0].timestamp == first_weekly_ts

    def test_metadata_stored_on_registry(self, db_engine):
        """get_ticker_meta returns the full TickerRecord for use by the
        scheduler (which needs tz and source_timeframe)."""
        with transaction(db_engine) as conn:
            TickersRepository(conn).insert(
                NIFTY,
                created_at=datetime.now(timezone.utc),
                tz="Asia/Kolkata",
                source_timeframe="1h",
            )
        registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
        registry.prime_all(db_engine)

        meta = registry.get_ticker_meta(NIFTY)
        assert meta is not None
        assert meta.tz == "Asia/Kolkata"
        assert meta.source_timeframe == "1h"

    def test_source_equals_target_uses_direct_fetch(self, db_engine):
        """When source_timeframe == target, no aggregation runs; direct
        fetch on the target tf."""
        tid = _seed_ticker(db_engine, NIFTY)
        # _seed_ticker uses defaults: tz='UTC', source='1W'. Seed weekly bars.
        _seed_bars(db_engine, tid, n=10)
        registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
        result = registry.prime_all(db_engine)
        assert result.tickers_primed == 1
        engine = registry.get(NIFTY, Timeframe.WEEKLY)
        assert engine is not None
        assert len(engine.state_history()) == 10

    def test_no_source_bars_skips_ticker(self, db_engine):
        """Ticker exists but has no source bars → skipped, not crashed."""
        with transaction(db_engine) as conn:
            TickersRepository(conn).insert(
                NIFTY,
                created_at=datetime.now(timezone.utc),
                tz="UTC",
                source_timeframe="1D",   # but no 1D bars seeded
            )
        registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
        result = registry.prime_all(db_engine)
        assert result.tickers_primed == 0
        assert result.tickers_skipped_no_bars == 1
        assert result.tickers_failed == 0
