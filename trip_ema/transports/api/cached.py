"""Cached read API — function (b) from the user's spec.

Per the architecture: the ComputeScheduler is the *only writer* to the
SignalCache. External callers and the SubscriberDistributor *read* from
the cache. This module exposes the read surface as a pair of functions
(verbose / terse).

If the requested ticker isn't in the cache, we raise — we don't try to
fall back to a fresh compute. The caller can either:
  - use compute_full_* (stateless module) if they have their own history, or
  - add the ticker to the DB and wait for the next ComputeScheduler tick.

This separation makes failure modes explicit. A bug that fills the cache
late won't be papered over by an expensive on-the-fly compute.
"""

from __future__ import annotations

from ..cache.signal_cache import SignalCache
from ..domain import TickerRef, Timeframe
from .responses import TerseResponse, TickerNotPrimedError, VerboseResponse


def query_cached_verbose(
    *,
    ticker: TickerRef,
    timeframe: Timeframe,
    cache: SignalCache,
    last_n_bars: int = 200,
) -> VerboseResponse:
    """Read the latest cached state for (ticker, timeframe). Returns the
    last `last_n_bars` from the cached state history."""
    signal = cache.get(ticker, timeframe)
    if signal is None:
        raise TickerNotPrimedError(
            f"No cached state for {ticker} @ {timeframe.value}. "
            "Either the ticker is not in the DB, not active, "
            "or the ComputeScheduler hasn't run yet."
        )
    history = signal.state_history
    if last_n_bars < len(history):
        history = history[-last_n_bars:]
    return VerboseResponse(
        ticker=signal.ticker,
        timeframe=signal.timeframe,
        bars=history,
        latest_change=signal.last_change,
        computed_at=signal.computed_at,
    )


def query_cached_terse(
    *,
    ticker: TickerRef,
    timeframe: Timeframe,
    cache: SignalCache,
) -> TerseResponse:
    """Read just the latest change for (ticker, timeframe)."""
    signal = cache.get(ticker, timeframe)
    if signal is None:
        raise TickerNotPrimedError(
            f"No cached state for {ticker} @ {timeframe.value}."
        )
    return TerseResponse(
        ticker=signal.ticker,
        timeframe=signal.timeframe,
        latest_bar_timestamp=signal.latest_state.timestamp,
        latest_change=signal.last_change,
        latest_price=signal.latest_close,
        computed_at=signal.computed_at,
    )
