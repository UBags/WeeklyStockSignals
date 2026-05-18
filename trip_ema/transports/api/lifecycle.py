"""Lifecycle — function (c) from the user's spec.

`prime_all_tickers` is the startup orchestrator. It:
  1. Builds TickerEngines for every active ticker × configured timeframe.
  2. Primes each engine with historical bars from the DB.
  3. Populates the SignalCache with the resulting latest states.

After this returns, the system is ready to serve cached queries (b) and
the ComputeScheduler can start its periodic compute loop.

Idempotent: calling prime_all_tickers a second time will rebuild the
registry from scratch. Useful for testing and for an admin "reload"
endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Engine

from ..cache.signal_cache import SignalCache, build_cached_signal
from ..domain import ChangeSignal, IndicatorParams, Timeframe
from ..persistence import BarsRepository
from ..registry.ticker_registry import PrimeResult, TickerRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """Summary of a startup prime, for logging and tests."""
    prime_result: PrimeResult
    cache_entries_populated: int


def prime_all_tickers(
    *,
    db_engine: Engine,
    registry: TickerRegistry,
    cache: SignalCache,
) -> LifecycleResult:
    """Wire up the warm registry and the read-mostly cache at startup.

    Args:
        db_engine: SQLAlchemy engine for the application database.
        registry: An *empty* (or to-be-replaced) TickerRegistry.
        cache: An *empty* (or to-be-replaced) SignalCache.

    Returns:
        Summary stats.
    """
    # Step 1: build and prime engines from the DB.
    prime_result = registry.prime_all(db_engine)

    # Step 2: for every engine that came out with a real BarState, seed
    # the cache. Engines that prime to an empty state (zero bars) are
    # already filtered out by registry.prime_all.
    cache_count = 0
    now = datetime.now(timezone.utc)

    for (ref, tf), engine in registry.all_engines():
        signal = _build_cached_signal_from_engine(ref, tf, engine, now)
        if signal is None:
            # Defensive: prime_all should have skipped these, but if it
            # didn't (e.g. all bars were NaN-producing during warmup),
            # don't seed the cache for them.
            continue
        cache.put(signal)
        cache_count += 1

    # Phase 10 observability: update gauges that reflect post-prime state
    from ..observability import metrics as M
    M.registry_size.set(len(list(registry.all_engines())))
    M.cache_size.set(len(cache))

    logger.info(
        "Lifecycle complete: %d engines, %d cache entries",
        prime_result.tickers_primed, cache_count,
    )
    return LifecycleResult(
        prime_result=prime_result,
        cache_entries_populated=cache_count,
    )


def _build_cached_signal_from_engine(ref, tf, engine, now):
    """Build a CachedSignal from a freshly-primed engine.

    Returns None if the engine has no state (which can happen if every
    bar in the input was NaN-producing — possible during warmup of a
    ticker with very little history). Callers should skip the cache
    write in that case.

    Used by both prime_all_tickers (startup) and the
    /v1/admin/reprime-ticker route (admin-triggered single-ticker
    re-prime). The cached signal is synthesized as 'no change since
    priming' — we don't know what the previous compute saw, so we
    treat the prime as a fresh observation.
    """
    latest = engine.latest_state()
    if latest is None:
        return None
    return build_cached_signal(
        ticker=ref,
        timeframe=tf,
        latest_state=latest,
        latest_close=engine.last_close() or float("nan"),
        state_history=engine.state_history(),
        last_change=ChangeSignal.NO_CHANGE,
        last_change_bar=None,
        last_change_computed_at=None,
        computed_at=now,
    )
