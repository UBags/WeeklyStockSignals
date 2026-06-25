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

from ....api.lifecycle import (
    PrimeCacheResult,
    _load_latest_indicator_states,
    _load_latest_signal_events,
    prime_cache_from_db,
)
from ....cache import SignalCache
from ....cache.signal_cache import CachedSnapshot
from ....config import Config
from ....domain import CoreRegime, TickerRef, Timeframe
from ....notifications.circuit_breaker import CircuitBreakingTransport
from ....scheduler import ComputeScheduler, SubscriberDistributor
from ..dependencies import (
    get_cache,
    get_compute_scheduler,
    get_config,
    get_db_engine,
    get_distributor,
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


@router.post("/compute-now", response_model=ComputeRunSummaryPayload)
def admin_compute_now(
    scheduler: ComputeScheduler = Depends(get_compute_scheduler),
) -> ComputeRunSummaryPayload:
    """Trigger an immediate `ComputeScheduler.run_once()`.

    Same code path as the background thread — cache is updated,
    events are persisted, summary is returned."""
    logger.info("Admin: compute-now requested")
    summary = scheduler.run_once()
    return ComputeRunSummaryPayload(
        started_at=summary.started_at,
        finished_at=summary.finished_at,
        tickers_examined=summary.tickers_examined,
        tickers_with_new_bars=summary.tickers_with_new_event,
        bars_ingested=0,
        events_fired=len(summary.events_fired),
        errors=summary.errors,
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

    # Re-read just this ticker's slice of indicator_state +
    # signal_events. The shape is the same as prime_cache_from_db
    # but filtered.
    with db_engine.connect() as conn:
        all_ind = _load_latest_indicator_states(conn, timeframe=timeframe)
        all_sig = _load_latest_signal_events(conn, timeframe=timeframe)

    ind_snap = all_ind.get(ticker.ticker_id)
    sig_snap = all_sig.get(ticker.ticker_id)

    if ind_snap is None and sig_snap is None:
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
        latest_signal_event=sig_snap,
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


@router.get(
    "/circuit-breakers",
    response_model=CircuitBreakersPayload,
)
def admin_circuit_breakers(
    config: Config = Depends(get_config),
    distributor: SubscriberDistributor | None = Depends(get_distributor),
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

    if distributor is None:
        return CircuitBreakersPayload(
            mode="legacy",
            breakers=[],
            snapshot_taken_at=now,
            notes="No distributor configured.",
        )

    entries: list[CircuitBreakerEntry] = []
    for _name, transport in distributor.transports.items():
        if not isinstance(transport, CircuitBreakingTransport):
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
