"""Tests for the rewritten `api/stateless.py`.

The OLD module built a throwaway `TickerEngine`. The NEW module
uses `compute_regime` + `generate_signal` directly. The HTTP route
`/v1/compute` keeps working through this transition; these tests
pin its behaviour.

Pure unit tests — no DB, no FastAPI. Synthetic bars are crafted to
exercise specific regime transitions deterministically (same shapes
as the Step 5 walker tests).
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone, UTC
from zoneinfo import ZoneInfo

import pytest

from trip_ema.api.stateless import (
    compute_full_terse,
    compute_full_verbose,
)
from trip_ema.domain import (
    ChangeSignal,
    CoreRegime,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from trip_ema.domain import IndicatorParams


_IST = ZoneInfo("Asia/Kolkata")
_TICKER = TickerRef(exchange="NSE", symbol="SYNTH", instrument="EQ")


def _weekly_bar(
    monday: date, *, close: float,
    hl_spread: float = 0.5, vol: float = 1_000_000.0,
) -> OHLCVBar:
    """A synthetic WEEKLY bar timestamped at IST Monday."""
    ts_ist = datetime.combine(monday, time(0, 0), tzinfo=_IST)
    half = hl_spread / 2
    return OHLCVBar(
        timestamp=ts_ist.astimezone(UTC),
        open=close, high=close + half, low=close - half, close=close,
        volume=vol,
    )


def _flat_weekly(start_monday: date, n_weeks: int, close: float,
                 hl_spread: float = 0.5) -> list[OHLCVBar]:
    return [
        _weekly_bar(start_monday + timedelta(weeks=i), close=close,
                    hl_spread=hl_spread)
        for i in range(n_weeks)
    ]


def _ramp_weekly(start_monday: date, n_weeks: int,
                 *, p0: float, p1: float,
                 hl_spread: float = 0.5) -> list[OHLCVBar]:
    out = []
    for i in range(n_weeks):
        frac = i / max(1, n_weeks - 1)
        price = p0 + frac * (p1 - p0)
        out.append(_weekly_bar(start_monday + timedelta(weeks=i),
                              close=price, hl_spread=hl_spread))
    return out


# ===========================================================================
# compute_full_verbose
# ===========================================================================


class TestComputeFullVerbose:

    def test_empty_history_with_one_bar_returns_one_state(self):
        latest = _weekly_bar(date(2025, 6, 9), close=100.0)
        result = compute_full_verbose(
            ticker=_TICKER, history=[], latest_bar=latest,
            timeframe=Timeframe.WEEKLY,
            params=IndicatorParams(),
        )
        # 1 bar — warmup, no real regime, but a BarState placeholder.
        assert len(result.bars) == 1
        assert result.latest_change == ChangeSignal.NO_CHANGE

    def test_warmup_only_emits_neutral_states(self):
        bars = _flat_weekly(date(2024, 1, 1), n_weeks=5, close=100.0)
        result = compute_full_verbose(
            ticker=_TICKER, history=bars[:-1], latest_bar=bars[-1],
            timeframe=Timeframe.WEEKLY,
            params=IndicatorParams(),
        )
        # All 5 bars in warmup → all NEUTRAL.
        assert all(s.display_bg == CoreRegime.NEUTRAL for s in result.bars)
        assert result.latest_change == ChangeSignal.NO_CHANGE

    def test_long_flat_then_crash_classifies_sell(self):
        # `_classify_regime_transition` looks only at the last two
        # bars. To exercise the SELL path we need the second-to-last
        # bar to be GREEN/NEUTRAL and the latest bar to be BEARISH.
        # Multi-week ramps push the BEARISH transition deeper into
        # history, so collapse the crash into a single sharp bar.
        history = _flat_weekly(date(2022, 1, 3), n_weeks=156, close=100.0)
        latest = _weekly_bar(date(2025, 1, 6), close=50.0, hl_spread=2.0)
        result = compute_full_verbose(
            ticker=_TICKER, history=history, latest_bar=latest,
            timeframe=Timeframe.WEEKLY,
            params=IndicatorParams(),
        )
        # Sanity: the latest bar should be BEARISH on a -50% gap.
        assert result.bars[-1].display_bg == CoreRegime.BEARISH
        # Second-to-last is the last flat bar — should NOT be BEARISH.
        assert result.bars[-2].display_bg != CoreRegime.BEARISH
        # Transition GREEN/NEUTRAL → BEARISH = SELL.
        assert result.latest_change == ChangeSignal.SELL

    def test_state_history_retain_clips_output(self):
        bars = _flat_weekly(date(2022, 1, 3), n_weeks=200, close=100.0)
        result = compute_full_verbose(
            ticker=_TICKER, history=bars[:-1], latest_bar=bars[-1],
            timeframe=Timeframe.WEEKLY,
            params=IndicatorParams(),
            state_history_retain=20,
        )
        assert len(result.bars) == 20

    def test_ticker_and_timeframe_preserved(self):
        latest = _weekly_bar(date(2025, 6, 9), close=100.0)
        result = compute_full_verbose(
            ticker=_TICKER, history=[], latest_bar=latest,
            timeframe=Timeframe.WEEKLY,
            params=IndicatorParams(),
        )
        assert result.ticker == _TICKER
        assert result.timeframe == Timeframe.WEEKLY

    def test_core_bg_equals_display_bg(self):
        """Under the unified regime model, core_bg == display_bg at
        the wire boundary."""
        bars = (
            _flat_weekly(date(2022, 1, 3), n_weeks=156, close=100.0)
            + _ramp_weekly(date(2025, 1, 6), n_weeks=4, p0=100.0, p1=70.0)
        )
        result = compute_full_verbose(
            ticker=_TICKER, history=bars[:-1], latest_bar=bars[-1],
            timeframe=Timeframe.WEEKLY,
            params=IndicatorParams(),
        )
        for bar in result.bars:
            assert bar.core_bg == bar.display_bg


# ===========================================================================
# compute_full_terse
# ===========================================================================


class TestComputeFullTerse:

    def test_empty_history_with_one_bar_returns_no_change(self):
        latest = _weekly_bar(date(2025, 6, 9), close=100.0)
        result = compute_full_terse(
            ticker=_TICKER, history=[], latest_bar=latest,
            timeframe=Timeframe.WEEKLY,
            params=IndicatorParams(),
        )
        assert result.latest_change == ChangeSignal.NO_CHANGE
        assert result.latest_price == 100.0
        assert result.latest_bar_timestamp == latest.timestamp

    def test_long_flat_then_crash_classifies_sell(self):
        history = _flat_weekly(date(2022, 1, 3), n_weeks=156, close=100.0)
        latest = _weekly_bar(date(2025, 1, 6), close=50.0, hl_spread=2.0)
        result = compute_full_terse(
            ticker=_TICKER, history=history, latest_bar=latest,
            timeframe=Timeframe.WEEKLY,
            params=IndicatorParams(),
        )
        assert result.latest_change == ChangeSignal.SELL
        # latest_price is the latest_bar's close.
        assert result.latest_price == 50.0
