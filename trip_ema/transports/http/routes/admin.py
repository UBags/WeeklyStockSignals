"""Admin endpoints — operational levers gated by a bearer token.

# Step 9c rewrite

The OLD admin surface sat on top of `TickerRegistry` + the
`CachedSignal` shape and exposed `prime_all_tickers` plus a
single-ticker `reprime` that rebuilt an in-memory engine. After
Step 9 demolition, neither exists. The new routes:

  - `POST /v1/admin/prime-all`            → `prime_cache_from_db`
  - `POST /v1/admin/compute-now`          → unchanged (calls
                                             `ComputeScheduler.run_once`)
  - `GET  /v1/admin/stale-tickers`        → `cache.all_stale_snapshots()`
  - `POST /v1/admin/reprime-ticker/{...}` → refresh-one cache entry
                                             from DB
  - `GET  /v1/admin/circuit-breakers`     → unchanged
"""

from __future__ import annotations

import logging
from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

# PHASE-3E: lifecycle + SignalCache + CachedSnapshot + get_cache +
# LifecycleResultPayload + StaleTickerEntry + CoreRegime imports removed
# (all consumed only by /prime-all + /stale-tickers's now-deleted rich
# payload path).
from ....cache.current_signals_cache import CurrentSignalsCache
from ....config import Config
from ....domain import TickerRef, Timeframe
from ....orchestrator import recompute_market
from ..dependencies import (
    get_v1_cache,
    get_config,
    get_db_engine,
    require_admin_token,
)
from ..schemas import (
    CircuitBreakersPayload,
    ComputeRunSummaryPayload,
    RepriumeResultPayload,
    StaleTickersPayload,
    TickerRefPayload,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_token)],
)


# PHASE-3E: admin_prime_all deleted. No callers (GUI never wired,
# no HTTP client method). SignalCache is gone; there's nothing to prime.


# === STEP 14B-E4-compute_now: /compute-now rewired to V1 recompute_market ===
@router.post("/compute-now", response_model=ComputeRunSummaryPayload)
def admin_compute_now(
    db_engine: Engine = Depends(get_db_engine),
    v1_cache: CurrentSignalsCache = Depends(get_v1_cache),
) -> ComputeRunSummaryPayload:
    """Trigger an immediate V1 `recompute_market()` cycle.

    Same code path the orchestrator pool fires once per tick. NSE-only
    for now (V1 cycle is NSE-only; multi-exchange is Phase 14.7+ work).
    """
    from datetime import datetime, UTC
    started_at = datetime.now(UTC)
    logger.info("Admin: compute-now requested (V1 recompute_market)")
    result = recompute_market(
        db_engine,
        mode="live",
        cache=v1_cache,
        exchange="NSE",
    )
    finished_at = datetime.now(UTC)
    # RecomputeMarketResult fields → ComputeRunSummaryPayload mapping.
    # The V1 result shape differs from the legacy summary, so we map
    # what we can and zero the rest. Tier 3 may revisit the payload
    # shape itself.
    return ComputeRunSummaryPayload(
        started_at=started_at,
        finished_at=finished_at,
        tickers_examined=getattr(result, "tickers_examined", 0),
        tickers_with_new_bars=getattr(result, "signals_emitted", 0),
        bars_ingested=0,
        events_fired=getattr(result, "signals_emitted", 0),
        errors=getattr(result, "errors", []),
    )


@router.get("/stale-tickers", response_model=StaleTickersPayload)
def admin_stale_tickers() -> StaleTickersPayload:  # PHASE-3E: cache dep dropped
    """PHASE-3D: legacy stale-ticker tracking retired.

    Previously listed each ticker whose SignalCache entry carried
    a `stale_since` flag set by the compute scheduler when it saw
    an ETL gap. Post-Phase-3b nothing calls `cache.mark_stale`
    anywhere; post-Phase-3d the `latest_indicator_state` field is
    gone too — so the payload can't be built even if a stale entry
    existed. Returns an empty list; kept as a 200 endpoint so any
    external monitor polling this doesn't start alarming.
    """
    return StaleTickersPayload(count=0, entries=[])


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
    # PHASE-3E: cache dep dropped (SignalCache deleted).
) -> RepriumeResultPayload:
    """Refresh a single ticker's cache entry from DB.

    Under the OLD scheduler this rebuilt the in-memory TickerEngine.
    Under the new model there's no per-ticker state to rebuild — the
    cache holds the latest persisted snapshot, and the live tick
    path reloads bars each pass. So the only meaningful action here
    is to re-read this ticker's indicator_state and signal_events,
    construct a fresh CachedSnapshot, put_snapshot it, and clear any
    stale flag.

    Returns 404 if the ticker doesn't exist in the `tickers` table.
    Returns 200 with `succeeded=False` if the ticker exists but has
    no persisted state (nothing to cache — operator should trigger
    a backfill instead).
    """
    ref = TickerRef(exchange=exchange, symbol=symbol, instrument=instrument)
    logger.info(
        "Admin: reprime-ticker requested for %s@%s",
        ref, timeframe.value,
    )

    # Resolve ticker_id from ref.
    from ....persistence.tickers_repo import TickersRepository
    with db_engine.connect() as conn:
        ticker = TickersRepository(conn).get_by_ref(ref)
    if ticker is None:
        raise HTTPException(
            status_code=404,
            detail=f"Ticker not found: {ref}",
        )

    # PHASE-3D: legacy indicator_state cache retired. This endpoint's
    # original job (re-read one ticker's indicator_state row and
    # rebuild its CachedSnapshot) is meaningless now — the source
    # table was dropped in migration 0032 and the snapshot field it
    # populated was removed from CachedSnapshot. Kept as a 200 endpoint
    # for GUI-button compat; the notes tell the operator what to do
    # instead.
    return RepriumeResultPayload(
        ticker=TickerRefPayload.from_domain(ref),
        timeframe=timeframe.value,
        succeeded=True,
        bars_loaded=0,
        refreshed_metadata=False,
        notes=(
            "Reprime is a no-op post-Phase-3d. The legacy "
            "indicator_state cache was retired; V1 current_signals "
            "cache reflects the DB directly and is refreshed by the "
            "recompute cycle (POST /v1/admin/compute-now or the "
            "scheduled RunScheduler tick)."
        ),
    )


# === STEP 14B-E4-cb_sig: /circuit-breakers no longer takes a distributor ===
@router.get(
    "/circuit-breakers",
    response_model=CircuitBreakersPayload,
)
def admin_circuit_breakers(
    config: Config = Depends(get_config),
) -> CircuitBreakersPayload:
    """Return the current circuit-breaker state for each transport."""
    now = datetime.now(UTC)
    cb_cfg = config.transports.circuit_breaker

    if not cb_cfg.enabled:
        return CircuitBreakersPayload(
            mode="disabled",
            breakers=[],
            snapshot_taken_at=now,
            notes="Circuit breakers are disabled via "
                  "transports.circuit_breaker.enabled=False in config.",
        )

    if config.redis is not None:
        return CircuitBreakersPayload(
            mode="queue",
            breakers=[],
            snapshot_taken_at=now,
            notes="Breakers live in the notifier worker process. Use "
                  "its /metrics endpoint or scrape Prometheus directly "
                  "to inspect transport_circuit_state{transport,state}.",
        )

    # === STEP 14B-E4-cb_body: in-process distributor removed ===
    # The legacy SubscriberDistributor was demolished in Step 14b.
    # When Redis is configured, breakers live in the notifier worker
    # process (handled above). When Redis is not configured, V1
    # email sends go through notify_subscribers without circuit
    # breakers — that's an explicit V1 design choice (Brevo's own
    # rate-limiting + retry handles transient failures).
    return CircuitBreakersPayload(
        mode="legacy_removed",
        breakers=[],
        snapshot_taken_at=now,
        notes=(
            "In-process distributor was demolished in V1 cutover. "
            "When Redis is configured, breakers live in the notifier "
            "worker process — scrape Prometheus directly to inspect "
            "transport_circuit_state{transport,state}."
        ),
    )
