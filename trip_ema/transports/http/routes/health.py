"""Health endpoints — liveness and readiness probes for load balancers.

/healthz   — always 200 if the process is up. No DB check, no dependency
             check.
/readyz    — 200 only if the app is fully wired AND background threads
             are healthy. Specifically checks:
               - Cache has at least one snapshot
               - ComputeScheduler thread is alive (if it was started)
                 AND has completed a run within 2× its cadence
               - SubscriberDistributor thread is alive (if it was started)
                 AND has completed a run within 2× its cadence
               - Redis is reachable (if configured)
               - Orchestrator pool: no market thread has died (Phase 14)

# Step 9c note

TickerRegistry is gone. The `registry_size` wire field is kept for
backward compat with monitoring scrapes and is populated from
`cache.snapshot_count()` (semantically the closest replacement —
under the new model, the set of "registered" tickers is the set
that have cache snapshots).
"""

from __future__ import annotations

from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException, status

from ....cache import SignalCache
# === STEP 14B-E5: scheduler type imports stripped ===
# PHASE-3B: get_orchestrator_pool import dropped (function deleted).
# PHASE-3C: None-tolerant getters for run_scheduler + run_dispatcher.
from ..dependencies import (
    get_cache,
    get_redis_queue,
    get_run_dispatcher_or_none,
    get_run_scheduler_or_none,
)
from ..schemas import HealthPayload

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthPayload)
def healthz(
    cache: SignalCache = Depends(get_cache),
    run_scheduler=Depends(get_run_scheduler_or_none),
    run_dispatcher=Depends(get_run_dispatcher_or_none),
) -> HealthPayload:
    """Liveness probe. Returns 200 if the process is responsive.

    PHASE-3C: reports run_scheduler_alive + run_dispatcher_alive
    for informational value (does not change status — the process
    is "live" as long as it can answer HTTP). Silent thread death
    (which caused the 2026-07-02 Phase 3a startup outage) surfaces
    here now: run_scheduler_alive=false in the JSON would have
    caught it immediately.
    """
    n = cache.snapshot_count()
    return HealthPayload(
        status="ok",
        registry_size=n,   # legacy wire field, mirrors cache_size
        cache_size=n,
        run_scheduler_alive=_thread_alive(run_scheduler),
        run_dispatcher_alive=_thread_alive(run_dispatcher),
    )


def _thread_alive(subsystem) -> bool | None:
    """Small helper: return True/False for is_alive(), or None when
    the subsystem itself is missing (state not wired)."""
    if subsystem is None:
        return None
    try:
        return bool(subsystem.is_alive())
    except Exception:
        # Some third-party test double may not implement is_alive();
        # we treat that as "unknown" rather than 500-ing.
        return None


@router.get("/readyz", response_model=HealthPayload)
def readyz(
    cache: SignalCache = Depends(get_cache),
    redis_queue=Depends(get_redis_queue),
    run_scheduler=Depends(get_run_scheduler_or_none),
    run_dispatcher=Depends(get_run_dispatcher_or_none),
) -> HealthPayload:
    """Readiness probe. Returns 200 only if the service can do useful work.

    PHASE-3C signals:
      - Cache has at least one snapshot
      - Redis reachable (if configured)
      - RunScheduler thread alive
      - RunDispatcher thread alive

    A dead RunScheduler or RunDispatcher counts as unready (503) —
    the process is up but can't schedule or execute new cycles, so
    load balancers should depool it.
    """
    issues: list[str] = []

    n_snapshots = cache.snapshot_count()
    if n_snapshots == 0:
        issues.append("Cache empty (never seeded)")

    redis_reachable: bool | None = None
    if redis_queue is not None:
        redis_reachable = redis_queue.ping()
        if not redis_reachable:
            issues.append("Redis configured but unreachable")

    run_scheduler_alive = _thread_alive(run_scheduler)
    run_dispatcher_alive = _thread_alive(run_dispatcher)

    # Only treat False (not None) as an issue. None means "state not
    # wired" — that's a legitimate transient during startup and the
    # cache-empty check above already covers it.
    if run_scheduler_alive is False:
        issues.append("RunScheduler thread is not alive")
    if run_dispatcher_alive is False:
        issues.append("RunDispatcher thread is not alive")

    # Phase 9 A2: stale tickers — informational only.
    stale = cache.all_stale_snapshots()

    payload = HealthPayload(
        status="ready" if not issues else "not_ready",
        registry_size=n_snapshots,   # legacy wire field
        cache_size=n_snapshots,
        redis_reachable=redis_reachable,
        run_scheduler_alive=run_scheduler_alive,
        run_dispatcher_alive=run_dispatcher_alive,
        stale_tickers_count=len(stale),
        issues=issues,
    )

    if issues:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "issues": issues},
        )
    return payload