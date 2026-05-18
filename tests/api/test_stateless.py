"""Tests for the stateless compute functions (function (a))."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.api import (
    TerseResponse,
    VerboseResponse,
    compute_full_terse,
    compute_full_verbose,
)
from trip_ema.domain import ChangeSignal, IndicatorParams, OHLCVBar, TickerRef, Timeframe


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")


def _bars(n: int, start_close: float = 100.0) -> list[OHLCVBar]:
    """Generate n weekly bars, each rising 1 point."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        OHLCVBar(
            timestamp=start + timedelta(weeks=i),
            open=start_close + i,
            high=start_close + i + 1,
            low=start_close + i - 1,
            close=start_close + i + 0.5,
            volume=1000.0,
        )
        for i in range(n)
    ]


def test_compute_full_verbose_returns_verbose_response():
    bars = _bars(80)
    history, latest = bars[:-1], bars[-1]
    response = compute_full_verbose(
        ticker=NIFTY,
        history=history,
        latest_bar=latest,
        timeframe=Timeframe.WEEKLY,
        params=IndicatorParams(),
    )
    assert isinstance(response, VerboseResponse)
    assert response.ticker == NIFTY
    assert response.timeframe == Timeframe.WEEKLY
    assert isinstance(response.bars, tuple)
    # state_history_retain defaults to 200; we have 80 bars total.
    assert len(response.bars) == 80
    # Last bar in response is the latest_bar we ingested.
    assert response.bars[-1].timestamp == latest.timestamp


def test_compute_full_terse_returns_terse_response():
    bars = _bars(80)
    history, latest = bars[:-1], bars[-1]
    response = compute_full_terse(
        ticker=NIFTY,
        history=history,
        latest_bar=latest,
        timeframe=Timeframe.WEEKLY,
        params=IndicatorParams(),
    )
    assert isinstance(response, TerseResponse)
    assert response.latest_bar_timestamp == latest.timestamp
    assert response.latest_price == latest.close
    assert isinstance(response.latest_change, ChangeSignal)


def test_compute_full_with_empty_history_only_one_bar():
    """Edge case: history is empty, only the latest_bar is provided.
    Should not crash. Latest change is NO_CHANGE (nothing to compare against)."""
    latest = _bars(1)[0]
    response = compute_full_terse(
        ticker=NIFTY,
        history=[],
        latest_bar=latest,
        timeframe=Timeframe.WEEKLY,
        params=IndicatorParams(),
    )
    assert response.latest_change == ChangeSignal.NO_CHANGE


def test_compute_full_with_history_retain_limits_output():
    """state_history_retain should cap the number of bars returned."""
    bars = _bars(100)
    history, latest = bars[:-1], bars[-1]
    response = compute_full_verbose(
        ticker=NIFTY,
        history=history,
        latest_bar=latest,
        timeframe=Timeframe.WEEKLY,
        params=IndicatorParams(),
        state_history_retain=20,
    )
    assert len(response.bars) == 20
    # Returned bars are the last 20 — assert the most recent is present.
    assert response.bars[-1].timestamp == latest.timestamp
