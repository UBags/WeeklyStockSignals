"""Health endpoints — liveness and readiness probes for load balancers.

/healthz   — always 200 if the process is up. No DB check, no dependency
             check. Used to detect 'is the Python process alive'.
/readyz    — 200 only if the app is fully wired AND all background
             threads are healthy. Specifically checks:
               - Registry and cache are non-empty
               - ComputeScheduler thread is alive (if it was started)
                 AND has completed a run within 2× its cadence
               - SubscriberDistributor thread is alive (if it was started)
                 AND has completed a run within 2× its cadence
               - Redis is reachable (if configured)
             Returns 503 if anything is wrong.

The thread-liveness checks address the Phase 7 A3 fix: a daemon thread
that dies (uncaught exception, etc.) leaves the FastAPI app responsive
to requests but serving stale data forever. Without these checks, ops
wouldn't know.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from ....cache import SignalCache
from ....registry import TickerRegistry
from ....scheduler import ComputeScheduler, SubscriberDistributor
from ..dependencies import (
    get_cache,
    get_compute_scheduler,
    get_distributor,
    get_orchestrator_pool,
    get_redis_queue,
    get_registry,
)
from ..schemas import HealthPayload

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthPayload)
def healthz(
    registry: TickerRegistry = Depends(get_registry),
    cache: SignalCache = Depends(get_cache),
) -> HealthPayload:
    """Liveness probe. Returns 200 if the process is responsive.
    Doesn't fail on empty registry/cache — a freshly-started instance
    with no DB content is still 'alive', just not yet 'ready'."""
    return HealthPayload(
        status="ok",
        registry_size=len(registry),
        cache_size=len(cache),
    )


@router.get("/readyz", response_model=HealthPayload)
def readyz(
    registry: TickerRegistry = Depends(get_registry),
    cache: SignalCache = Depends(get_cache),
    compute_scheduler: ComputeScheduler = Depends(get_compute_scheduler),
    distributor: SubscriberDistributor | None = Depends(get_distributor),
    redis_queue=Depends(get_redis_queue),
    orchestrator_pool=Depends(get_orchestrator_pool),
) -> HealthPayload:
    """Readiness probe. Returns 200 only if the service can do useful work.

    Failure modes (any one → 503):
      - Registry empty (never primed)
      - Cache empty (lifecycle never seeded it)
      - ComputeScheduler thread was started but is now dead
      - ComputeScheduler thread is alive but wedged (no completion in 2×
        cadence)
      - SubscriberDistributor thread was started but is now dead
      - SubscriberDistributor thread is alive but wedged
      - Redis configured but unreachable
      - Orchestrator pool: at least one MarketOrchestrator was started
        but its loop thread has died (Phase 14)
    """
    now = datetime.now(timezone.utc)
    issues: list[str] = []

    # Lifecycle: registry and cache populated.
    if len(registry) == 0:
        issues.append("Registry empty (never primed)")
    if len(cache) == 0:
        issues.append("Cache empty (never seeded)")

    # ComputeScheduler health. When the orchestrator is on, the
    # compute_scheduler is constructed but never started — its
    # `is_unhealthy()` returns None for the never-started case,
    # so this is correctly a no-op in orchestrator mode.
    compute_problem = compute_scheduler.is_unhealthy(now)
    if compute_problem:
        issues.append(compute_problem)

    # Distributor health (only if one is configured).
    if distributor is not None:
        dist_problem = distributor.is_unhealthy(now)
        if dist_problem:
            issues.append(dist_problem)

    # Redis reachability (only if queue is configured).
    redis_reachable: bool | None = None
    if redis_queue is not None:
        redis_reachable = redis_queue.ping()
        if not redis_reachable:
            issues.append("Redis configured but unreachable")

    # Phase 14: orchestrator pool health. None when orchestrator is
    # disabled in config; we don't penalize that case.
    if orchestrator_pool is not None and orchestrator_pool.any_unhealthy():
        issues.append(
            "Orchestrator pool: at least one market thread is "
            "unhealthy. Check /v1/admin/orchestrator/status for details.",
        )

    # Phase 9 A2: stale tickers — informational only.
    # Does NOT block readiness; it's an upstream-data concern, not a
    # service-health concern. Ops alerting should target the count.
    stale = cache.stale_signals()

    payload = HealthPayload(
        status="ready" if not issues else "not_ready",
        registry_size=len(registry),
        cache_size=len(cache),
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
