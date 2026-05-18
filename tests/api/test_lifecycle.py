"""Tests for prime_all_tickers (function (c)) — full startup orchestration.

These touch the DB because that's the function's job.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.api import prime_all_tickers
from trip_ema.cache import SignalCache
from trip_ema.domain import IndicatorParams, OHLCVBar, TickerRef, Timeframe
from trip_ema.persistence import BarsRepository, TickersRepository, transaction
from trip_ema.registry import TickerRegistry


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")
RELIANCE = TickerRef("NSE", "RELIANCE", "EQ")


def _seed_ticker_with_bars(db_engine, ref, n=50):
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(
            ref, created_at=datetime.now(timezone.utc),
        )
        bars_repo = BarsRepository(conn)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for i in range(n):
            bars_repo.insert_bar(
                tid, Timeframe.WEEKLY,
                OHLCVBar(
                    timestamp=start + timedelta(weeks=i),
                    open=100 + i, high=101 + i, low=99 + i, close=100.5 + i,
                    volume=1000.0,
                ),
            )
    return tid


def test_prime_all_populates_registry_and_cache(db_engine):
    _seed_ticker_with_bars(db_engine, NIFTY, n=60)
    _seed_ticker_with_bars(db_engine, RELIANCE, n=60)

    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    cache = SignalCache()

    result = prime_all_tickers(
        db_engine=db_engine, registry=registry, cache=cache,
    )

    assert result.prime_result.tickers_primed == 2
    assert result.cache_entries_populated == 2
    assert cache.get(NIFTY, Timeframe.WEEKLY) is not None
    assert cache.get(RELIANCE, Timeframe.WEEKLY) is not None


def test_prime_all_cache_entries_have_latest_state(db_engine):
    _seed_ticker_with_bars(db_engine, NIFTY, n=60)
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    cache = SignalCache()
    prime_all_tickers(db_engine=db_engine, registry=registry, cache=cache)

    signal = cache.get(NIFTY, Timeframe.WEEKLY)
    assert signal is not None
    assert signal.latest_state is not None
    # latest_close was tracked by the engine and should be a real number
    assert isinstance(signal.latest_close, float)
    assert not math.isnan(signal.latest_close)


def test_prime_all_empty_db_yields_empty_registry_and_cache(db_engine):
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    cache = SignalCache()
    result = prime_all_tickers(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    assert result.prime_result.tickers_primed == 0
    assert result.cache_entries_populated == 0
    assert len(cache) == 0


def test_prime_all_then_query_cached_works_end_to_end(db_engine):
    """Verify the (c)→(b) flow: prime at startup, then read from cache."""
    from trip_ema.api import query_cached_terse

    _seed_ticker_with_bars(db_engine, NIFTY, n=60)
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    cache = SignalCache()
    prime_all_tickers(db_engine=db_engine, registry=registry, cache=cache)

    response = query_cached_terse(
        ticker=NIFTY, timeframe=Timeframe.WEEKLY, cache=cache,
    )
    assert response.ticker == NIFTY
    # On initial prime, no change has occurred yet — last_change is NO_CHANGE.
    from trip_ema.domain import ChangeSignal
    assert response.latest_change == ChangeSignal.NO_CHANGE
