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

# === STEP 15-B10-import: _load_latest_signal_events removed ===
from ....api.lifecycle import (
    PrimeCacheResult,
    _load_latest_indicator_states,
    prime_cache_from_db,
)
# === STEP 14B-FIXUP1-admin-import: CurrentSignalsCache added ===
from ....cache import SignalCache
from ....cache.current_signals_cache import CurrentSignalsCache
from ....cache.signal_cache import CachedSnapshot
from ....config import Config
from ....domain import CoreRegime, TickerRef, Timeframe
from ....notifications.circuit_breaker import CircuitBreakingTransport
# === STEP 14B-E4-import: ComputeScheduler + SubscriberDistributor imports stripped ===
from ....orchestrator import recompute_market
# === STEP 14B-FIXUP1-admin-deps: get_v1_cache added ===
from ..dependencies import (
    get_cache,
    get_v1_cache,
    # === STEP 14B-E4-dep_cs: get_compute_scheduler removed ===
    get_config,
    get_db_engine,
    # === STEP 14B-E4-dep_dist: get_distributor removed ===
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
    cache: SignalCache = Depends(get_cache),
) -> LifecycleResultPayload:
    """Re-run the startup prime — two SELECTs against indicator_state
    and signal_events seed the cache. Idempotent; safe to call after
    bulk DB writes (regenerate-signals, manual SQL imports, etc.)."""
    logger.info("Admin: prime-all requested")
    result: PrimeCacheResult = prime_cache_from_db(
        db_engine=db_engine, cache=cache,
    )
    return LifecycleResultPayload(
        tickers_primed=result.snapshots_populated,
        tickers_skipped_no_bars=result.tickers_seen - result.snapshots_populated,
        tickers_failed=0,
        cache_entries_populated=result.snapshots_populated,
    )


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
def admin_stale_tickers(
    cache: SignalCache = Depends(get_cache),
) -> StaleTickersPayload:
    """List every ticker currently flagged stale due to an ETL gap.

    Each entry carries `stale_since` (first detection), the last good
    indicator_state's trade_date and computed_at, and the last known
    regime as a string.
    """
    stale = cache.all_stale_snapshots()
    entries: list[StaleTickerEntry] = []
    for s in stale:
        # Defensive: stale_snapshots can in principle have no
        # indicator yet. Skip those — they have nothing to display.
        if s.latest_indicator_state is None:
            continue
        # Map the IST-Monday DATE back to a UTC datetime for the
        # wire shape's `last_good_bar_timestamp: datetime`.
        bar_ts = datetime.combine(
            s.latest_indicator_state.trade_date,
            datetime.min.time(),
            tzinfo=UTC,
        )
        assert s.stale_since is not None, (
            "non-stale entries should have been skipped above; "
            "reached StaleTickerEntry append with stale_since=None"
        )
        entries.append(StaleTickerEntry(
            ticker=TickerRefPayload.from_domain(s.ticker_ref),
            timeframe=s.timeframe.value,
            stale_since=s.stale_since,
            last_good_bar_timestamp=bar_ts,
            last_good_computed_at=s.latest_indicator_state.computed_at,
            last_known_signal=CoreRegime(
                s.latest_indicator_state.display_regime,
            ).name,
        ))
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
    cache: SignalCache = Depends(get_cache),
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

    # === STEP 15-B10-body: all_sig + sig_snap removed (indicator-only) ===
    with db_engine.connect() as conn:
        all_ind = _load_latest_indicator_states(conn, timeframe=timeframe)

    ind_snap = all_ind.get(ticker.ticker_id)

    if ind_snap is None:
        return RepriumeResultPayload(
            ticker=TickerRefPayload.from_domain(ref),
            timeframe=timeframe.value,
            succeeded=False,
            bars_loaded=0,
            refreshed_metadata=False,
            notes=(
                "Ticker exists but has no persisted state. Run a "
                "backfill (CLI or admin endpoint) first."
            ),
        )

    cache.put_snapshot(CachedSnapshot(
        ticker_id=ticker.ticker_id,
        ticker_ref=ref,
        timeframe=timeframe,
        latest_indicator_state=ind_snap,
        # STEP 15-B10-snapshot: latest_signal_event arg removed
        snapshot_taken_at=datetime.now(UTC),
        stale_since=None,  # explicit clear on reprime
    ))

    return RepriumeResultPayload(
        ticker=TickerRefPayload.from_domain(ref),
        timeframe=timeframe.value,
        succeeded=True,
        bars_loaded=1 if ind_snap is not None else 0,
        refreshed_metadata=False,
        notes="Cache entry refreshed from DB; stale flag cleared.",
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
