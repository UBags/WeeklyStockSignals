"""Admin endpoints — operational levers gated by a bearer token.

POST /v1/admin/prime-all                          → re-runs prime_all_tickers
POST /v1/admin/compute-now                        → forces ComputeScheduler.run_once
GET  /v1/admin/stale-tickers                      → lists tickers flagged stale
POST /v1/admin/reprime-ticker/{exchange}/{symbol} → reprime a single ticker
                                                    (refreshes metadata from DB)
GET  /v1/admin/circuit-breakers                   → current breaker state per transport
                                                    (legacy mode only; queue mode says so)

Token configuration is in dependencies.require_admin_token — endpoints
return 503 if the token env var is unset, 401 if no header, 403 if wrong.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

from ....api import prime_all_tickers
from ....api.lifecycle import _build_cached_signal_from_engine
from ....cache import SignalCache
from ....config import Config
from ....domain import CoreRegime, TickerRef, Timeframe
from ....notifications.circuit_breaker import CircuitBreakingTransport
from ....registry import TickerRegistry
from ....scheduler import ComputeScheduler, SubscriberDistributor
from ..dependencies import (
    get_cache,
    get_compute_scheduler,
    get_config,
    get_db_engine,
    get_distributor,
    get_registry,
    require_admin_token,
)
from ..schemas import (
    CircuitBreakerConfigPayload,
    CircuitBreakerEntry,
    CircuitBreakersPayload,
    ComputeRunSummaryPayload,
    LifecycleResultPayload,
    RepriumeResultPayload,
    StaleTickerEntry,
    StaleTickersPayload,
    TickerRefPayload,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_token)],
)


@router.post("/prime-all", response_model=LifecycleResultPayload)
def admin_prime_all(
    db_engine: Engine = Depends(get_db_engine),
    registry: TickerRegistry = Depends(get_registry),
    cache: SignalCache = Depends(get_cache),
) -> LifecycleResultPayload:
    """Re-run the startup prime over the registry and cache.

    Idempotent — re-primes every ticker from DB. Useful after manual
    DB updates (new tickers, bulk historical backfills, etc.)."""
    logger.info("Admin: prime-all requested")
    result = prime_all_tickers(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    return LifecycleResultPayload(
        tickers_primed=result.prime_result.tickers_primed,
        tickers_skipped_no_bars=result.prime_result.tickers_skipped_no_bars,
        tickers_failed=result.prime_result.tickers_failed,
        cache_entries_populated=result.cache_entries_populated,
    )


@router.post("/compute-now", response_model=ComputeRunSummaryPayload)
def admin_compute_now(
    scheduler: ComputeScheduler = Depends(get_compute_scheduler),
) -> ComputeRunSummaryPayload:
    """Trigger an immediate ComputeScheduler.run_once().

    Note: this calls the same method the background thread uses, so the
    semantics are identical to a normal scheduled compute. The cache is
    updated, events are persisted, etc."""
    logger.info("Admin: compute-now requested")
    summary = scheduler.run_once()
    return ComputeRunSummaryPayload(
        started_at=summary.started_at,
        finished_at=summary.finished_at,
        tickers_examined=summary.tickers_examined,
        tickers_with_new_bars=summary.tickers_with_new_bars,
        bars_ingested=summary.bars_ingested,
        events_fired=len(summary.events_fired),
        errors=summary.errors,
    )


@router.get("/stale-tickers", response_model=StaleTickersPayload)
def admin_stale_tickers(
    cache: SignalCache = Depends(get_cache),
) -> StaleTickersPayload:
    """Phase 9 A2: list every ticker currently flagged stale due to an
    ETL data gap. Each entry carries:

      - The last-known-good BarState's timestamp (so ops can see how
        far behind the data is).
      - The wall-clock moment the gap was first detected (`stale_since`).
      - The last known signal (so the trader gets meaningful context
        even while waiting for backfill).

    Empty list means no stale tickers — all ETL pipelines are current.
    """
    stale = cache.stale_signals()
    entries = [
        StaleTickerEntry(
            ticker=TickerRefPayload.from_domain(s.ticker),
            timeframe=s.timeframe.value,
            stale_since=s.stale_since,
            last_good_bar_timestamp=s.latest_state.timestamp,
            last_good_computed_at=s.computed_at,
            last_known_signal=CoreRegime(s.latest_state.display_bg).name,
        )
        for s in stale
    ]
    return StaleTickersPayload(count=len(entries), entries=entries)


@router.post(
    "/reprime-ticker/{exchange}/{symbol}",
    response_model=RepriumeResultPayload,
)
def admin_reprime_ticker(
    exchange: str,
    symbol: str,
    instrument: str = Query(default="EQ", max_length=16),
    timeframe: Timeframe = Query(default=Timeframe.WEEKLY),
    db_engine: Engine = Depends(get_db_engine),
    registry: TickerRegistry = Depends(get_registry),
    cache: SignalCache = Depends(get_cache),
) -> RepriumeResultPayload:
    """Reprime a single (ticker, timeframe). The registry's metadata
    cache for this ticker is force-refreshed from the DB before the
    re-aggregation, so any operator changes to `tickers.tz` or
    `tickers.source_timeframe` get picked up — that's the usual reason
    to hit this endpoint rather than restarting the service.

    The cache entry for the ticker is replaced with a fresh
    `CachedSignal` synthesized from the rebuilt engine. Any prior
    `stale_since` flag is cleared as a side effect.

    Returns 404 if the ticker isn't in the registry. Returns 200 with
    `succeeded=False` if the ticker is known but has no bars in the DB
    (the engine is then removed from the registry — same semantics as
    `registry.reprime`).
    """
    ref = TickerRef(exchange=exchange, symbol=symbol, instrument=instrument)
    logger.info(
        "Admin: reprime-ticker requested for %s@%s",
        ref, timeframe.value,
    )
    try:
        succeeded = registry.reprime(
            ref, timeframe, db_engine, refresh_metadata=True,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    if not succeeded:
        return RepriumeResultPayload(
            ticker=TickerRefPayload.from_domain(ref),
            timeframe=timeframe.value,
            succeeded=False,
            bars_loaded=0,
            refreshed_metadata=True,
            notes="Ticker exists but has no bars in DB; engine removed from registry.",
        )

    # Refresh the cache entry from the rebuilt engine. Without this, the
    # cache would still hold the old (and potentially stale-flagged)
    # CachedSignal until the next compute pass.
    from datetime import datetime, timezone
    engine = registry.get(ref, timeframe)
    if engine is not None:
        new_signal = _build_cached_signal_from_engine(
            ref, timeframe, engine, datetime.now(timezone.utc),
        )
        if new_signal is not None:
            cache.put(new_signal)

    return RepriumeResultPayload(
        ticker=TickerRefPayload.from_domain(ref),
        timeframe=timeframe.value,
        succeeded=True,
        bars_loaded=len(engine.state_history()) if engine else 0,
        refreshed_metadata=True,
        notes="Engine rebuilt; cache entry refreshed; any prior stale flag cleared.",
    )


@router.get(
    "/circuit-breakers",
    response_model=CircuitBreakersPayload,
)
def admin_circuit_breakers(
    config: Config = Depends(get_config),
    distributor: SubscriberDistributor | None = Depends(get_distributor),
) -> CircuitBreakersPayload:
    """Return the current circuit-breaker state for each transport.

    Useful during an incident: curl this endpoint to see "is the email
    transport's breaker OPEN right now?" without having to scrape
    Prometheus. Complements the trip_ema_transport_circuit_state /
    trip_ema_transport_circuit_transitions_total metrics (which remain
    the right tool for dashboards and alerts).

    Three modes:
      - 'disabled':  breakers turned off in config; no state to report.
      - 'legacy':    transports are dispatched in-process by the
                     distributor; we can read the breaker map directly.
      - 'queue':     the notifier worker runs in a separate process and
                     holds the breakers. We can't reach them from here;
                     scrape the notifier process's metrics endpoint or
                     the Prometheus push gateway.
    """
    now = datetime.now(timezone.utc)
    cb_cfg = config.transports.circuit_breaker

    if not cb_cfg.enabled:
        return CircuitBreakersPayload(
            mode="disabled",
            breakers=[],
            snapshot_taken_at=now,
            notes="Circuit breakers are disabled via "
                  "transports.circuit_breaker.enabled=False in config.",
        )

    # If a Redis queue is configured the distributor runs in queue mode
    # and breakers live in the notifier subprocess (not here).
    if config.redis is not None:
        return CircuitBreakersPayload(
            mode="queue",
            breakers=[],
            snapshot_taken_at=now,
            notes="Breakers live in the notifier worker process. Use "
                  "its /metrics endpoint or scrape Prometheus directly "
                  "to inspect transport_circuit_state{transport,state}.",
        )

    # Legacy in-process mode: distributor holds the transports map
    if distributor is None:
        return CircuitBreakersPayload(
            mode="legacy",
            breakers=[],
            snapshot_taken_at=now,
            notes="No distributor configured.",
        )

    entries: list[CircuitBreakerEntry] = []
    for name, transport in distributor.transports.items():
        if not isinstance(transport, CircuitBreakingTransport):
            # A transport not wrapped by a breaker — skip rather than
            # invent state. This shouldn't happen in production
            # (_wrap_transports_with_breakers in app.py wraps them all
            # when enabled=True), but defensive.
            continue
        snap = transport.snapshot()
        entries.append(CircuitBreakerEntry(
            name=snap["name"],
            state=snap["state"],
            consecutive_failures=snap["consecutive_failures"],
            opened_at_monotonic=snap["opened_at_monotonic"],
            config=CircuitBreakerConfigPayload(**snap["config"]),
        ))

    return CircuitBreakersPayload(
        mode="legacy",
        breakers=entries,
        snapshot_taken_at=now,
        notes=f"Reporting {len(entries)} breaker(s) from in-process distributor.",
    )
