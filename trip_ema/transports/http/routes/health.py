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


from fastapi import APIRouter, Depends, HTTPException, status

# PHASE-3E: SignalCache import + get_cache removed (subsystem deleted).
from ..dependencies import (
    get_redis_queue,
    get_run_dispatcher_or_none,
    get_run_scheduler_or_none,
)
from ..schemas import HealthPayload

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthPayload)
def healthz(
    run_scheduler=Depends(get_run_scheduler_or_none),
    run_dispatcher=Depends(get_run_dispatcher_or_none),
) -> HealthPayload:
    """Liveness probe. Returns 200 if the process is responsive.

    PHASE-3E: cache-derived fields (registry_size, cache_size) removed
    alongside SignalCache deletion. run_scheduler_alive +
    run_dispatcher_alive remain as informational subsystem status.
    """
    return HealthPayload(
        status="ok",
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
    redis_queue=Depends(get_redis_queue),
    run_scheduler=Depends(get_run_scheduler_or_none),
    run_dispatcher=Depends(get_run_dispatcher_or_none),
) -> HealthPayload:
    """Readiness probe. Returns 200 only if the service can do useful work.

    PHASE-3E signals:
      - Redis reachable (if configured)
      - RunScheduler thread alive
      - RunDispatcher thread alive

    A dead RunScheduler or RunDispatcher counts as unready (503) —
    the process is up but can't schedule or execute new cycles, so
    load balancers should depool it. The former "cache empty" and
    "stale tickers" signals were removed alongside SignalCache.
    """
    issues: list[str] = []

    redis_reachable: bool | None = None
    if redis_queue is not None:
        redis_reachable = redis_queue.ping()
        if not redis_reachable:
            issues.append("Redis configured but unreachable")

    run_scheduler_alive = _thread_alive(run_scheduler)
    run_dispatcher_alive = _thread_alive(run_dispatcher)

    if run_scheduler_alive is False:
        issues.append("RunScheduler thread is not alive")
    if run_dispatcher_alive is False:
        issues.append("RunDispatcher thread is not alive")

    payload = HealthPayload(
        status="ready" if not issues else "not_ready",
        redis_reachable=redis_reachable,
        run_scheduler_alive=run_scheduler_alive,
        run_dispatcher_alive=run_dispatcher_alive,
        issues=issues,
    )

    if issues:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "issues": issues},
        )
    return payload