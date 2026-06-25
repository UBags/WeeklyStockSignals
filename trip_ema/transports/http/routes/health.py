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
from ....scheduler import ComputeScheduler, SubscriberDistributor
from ..dependencies import (
    get_cache,
    get_compute_scheduler,
    get_distributor,
    get_orchestrator_pool,
    get_redis_queue,
)
from ..schemas import HealthPayload

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthPayload)
def healthz(
    cache: SignalCache = Depends(get_cache),
) -> HealthPayload:
    """Liveness probe. Returns 200 if the process is responsive."""
    n = cache.snapshot_count()
    return HealthPayload(
        status="ok",
        registry_size=n,   # legacy wire field, mirrors cache_size
        cache_size=n,
    )


@router.get("/readyz", response_model=HealthPayload)
def readyz(
    cache: SignalCache = Depends(get_cache),
    compute_scheduler: ComputeScheduler = Depends(get_compute_scheduler),
    distributor: SubscriberDistributor | None = Depends(get_distributor),
    redis_queue=Depends(get_redis_queue),
    orchestrator_pool=Depends(get_orchestrator_pool),
) -> HealthPayload:
    """Readiness probe. Returns 200 only if the service can do useful work."""
    now = datetime.now(UTC)
    issues: list[str] = []

    n_snapshots = cache.snapshot_count()
    if n_snapshots == 0:
        issues.append("Cache empty (never seeded)")

    compute_problem = compute_scheduler.is_unhealthy(now)
    if compute_problem:
        issues.append(compute_problem)

    if distributor is not None:
        dist_problem = distributor.is_unhealthy(now)
        if dist_problem:
            issues.append(dist_problem)

    redis_reachable: bool | None = None
    if redis_queue is not None:
        redis_reachable = redis_queue.ping()
        if not redis_reachable:
            issues.append("Redis configured but unreachable")

    if orchestrator_pool is not None and orchestrator_pool.any_unhealthy():
        issues.append(
            "Orchestrator pool: at least one market thread is "
            "unhealthy. Check /v1/admin/orchestrator/status for details.",
        )

    # Phase 9 A2: stale tickers — informational only.
    stale = cache.all_stale_snapshots()

    payload = HealthPayload(
        status="ready" if not issues else "not_ready",
        registry_size=n_snapshots,   # legacy wire field
        cache_size=n_snapshots,
        compute_scheduler_alive=compute_scheduler.is_alive(),
        distributor_alive=distributor.is_alive() if distributor else None,
        compute_last_completed_at=compute_scheduler.last_run_completed_at,
        distributor_last_completed_at=(
            distributor.last_run_completed_at if distributor else None
        ),
        redis_reachable=redis_reachable,
        stale_tickers_count=len(stale),
        issues=issues,
    )

    if issues:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "issues": issues},
        )
    return payload