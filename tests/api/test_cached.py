"""Tests for the cached query functions (function (b))."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trip_ema.api import (
    TickerNotPrimedError,
    query_cached_terse,
    query_cached_verbose,
)
from trip_ema.cache import SignalCache, build_cached_signal
from trip_ema.domain import (
    BarState,
    ChangeSignal,
    CoreRegime,
    TickerRef,
    Timeframe,
)


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")
RELIANCE = TickerRef("NSE", "RELIANCE", "EQ")


def _bar_state(t: datetime, display: CoreRegime = CoreRegime.BULLISH) -> BarState:
    return BarState(
        timestamp=t,
        med_ema=100.0, long_ema=100.0, longest_ema=100.0,
        atr=2.0,
        upper_inner=102, lower_inner=98,
        upper_mid=105, lower_mid=95,
        upper_outer=110, lower_outer=90,
        pierces_upper_mid=False, pierces_lower_mid=False,
        core_bg=display, display_bg=display,
    )


def _populated_cache(*, last_change: ChangeSignal = ChangeSignal.BUY) -> SignalCache:
    """Cache seeded with one entry for NIFTY."""
    cache = SignalCache()
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    history = [_bar_state(t)] * 5
    cache.put(build_cached_signal(
        ticker=NIFTY,
        timeframe=Timeframe.WEEKLY,
        latest_state=_bar_state(t),
        latest_close=24856.20,
        state_history=history,
        last_change=last_change,
        last_change_bar=t,
        last_change_computed_at=t,
        computed_at=t,
    ))
    return cache


def test_query_cached_verbose_returns_full_response():
    cache = _populated_cache()
    response = query_cached_verbose(
        ticker=NIFTY, timeframe=Timeframe.WEEKLY, cache=cache,
    )
    assert response.ticker == NIFTY
    assert response.latest_change == ChangeSignal.BUY
    assert len(response.bars) == 5


def test_query_cached_terse_returns_terse_response():
    cache = _populated_cache()
    response = query_cached_terse(
        ticker=NIFTY, timeframe=Timeframe.WEEKLY, cache=cache,
    )
    assert response.ticker == NIFTY
    assert response.latest_change == ChangeSignal.BUY
    assert response.latest_price == 24856.20


def test_query_missing_ticker_raises():
    cache = SignalCache()
    with pytest.raises(TickerNotPrimedError):
        query_cached_verbose(
            ticker=NIFTY, timeframe=Timeframe.WEEKLY, cache=cache,
        )
    with pytest.raises(TickerNotPrimedError):
        query_cached_terse(
            ticker=NIFTY, timeframe=Timeframe.WEEKLY, cache=cache,
        )


def test_query_missing_timeframe_for_present_ticker_raises():
    """Cache has NIFTY at WEEKLY; asking for DAILY should still raise."""
    cache = _populated_cache()
    with pytest.raises(TickerNotPrimedError):
        query_cached_terse(
            ticker=NIFTY, timeframe=Timeframe.DAILY, cache=cache,
        )


def test_last_n_bars_limits_verbose_history():
    """When the cache has 5 bars and last_n_bars=2, only the last 2 are returned."""
    cache = _populated_cache()
    response = query_cached_verbose(
        ticker=NIFTY, timeframe=Timeframe.WEEKLY,
        cache=cache, last_n_bars=2,
    )
    assert len(response.bars) == 2


def test_last_n_bars_larger_than_history_returns_all():
    cache = _populated_cache()
    response = query_cached_verbose(
        ticker=NIFTY, timeframe=Timeframe.WEEKLY,
        cache=cache, last_n_bars=1000,
    )
    assert len(response.bars) == 5
