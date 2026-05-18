"""Stateless compute — function (a) from the user's spec.

`compute_full` is the ad-hoc / testing entry point: caller provides the
ticker + full history + latest bar, gets back the verbose or terse response.
No cache consulted, no DB touched. A throwaway TickerEngine is built,
primed, and used for one call, then discarded.

This is intentionally heavier than the cached path. Use it for:
  - One-off analysis ("what would the signal have been if...")
  - Tickers not in the warm registry yet
  - Testing harnesses
  - External callers who manage their own bar storage
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..domain import IndicatorParams, OHLCVBar, TickerRef, Timeframe
from ..engine import TickerEngine
from ..engine.change_classifier import classify_change
from .responses import TerseResponse, VerboseResponse


def compute_full_verbose(
    *,
    ticker: TickerRef,
    history: list[OHLCVBar],
    latest_bar: OHLCVBar,
    timeframe: Timeframe,
    params: IndicatorParams,
    state_history_retain: int = 200,
) -> VerboseResponse:
    """Build a temporary engine, prime with `history`, ingest `latest_bar`,
    return the full state history as a VerboseResponse."""
    engine = TickerEngine(
        ticker, timeframe, params,
        history_retain=state_history_retain,
    )
    engine.prime(history)
    previous_display = (
        engine.latest_state().display_bg if engine.latest_state() else None
    )
    engine.ingest(latest_bar)
    latest = engine.latest_state()
    assert latest is not None   # we just ingested

    if previous_display is None:
        # First and only bar — no "previous" to compare against
        from ..domain import ChangeSignal
        change = ChangeSignal.NO_CHANGE
    else:
        change = classify_change(previous_display, latest.display_bg)

    return VerboseResponse(
        ticker=ticker,
        timeframe=timeframe,
        bars=tuple(engine.state_history()),
        latest_change=change,
        computed_at=datetime.now(timezone.utc),
    )


def compute_full_terse(
    *,
    ticker: TickerRef,
    history: list[OHLCVBar],
    latest_bar: OHLCVBar,
    timeframe: Timeframe,
    params: IndicatorParams,
) -> TerseResponse:
    """Same as verbose but returns only the latest-change summary."""
    engine = TickerEngine(ticker, timeframe, params, history_retain=2)
    engine.prime(history)
    previous_display = (
        engine.latest_state().display_bg if engine.latest_state() else None
    )
    engine.ingest(latest_bar)
    latest = engine.latest_state()
    assert latest is not None

    if previous_display is None:
        from ..domain import ChangeSignal
        change = ChangeSignal.NO_CHANGE
    else:
        change = classify_change(previous_display, latest.display_bg)

    return TerseResponse(
        ticker=ticker,
        timeframe=timeframe,
        latest_bar_timestamp=latest.timestamp,
        latest_change=change,
        latest_price=latest_bar.close,
        computed_at=datetime.now(timezone.utc),
    )
