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
from ..dependencies import (
    get_cache,
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
    redis_queue=Depends(get_redis_queue),
) -> HealthPayload:
    """Readiness probe. Returns 200 only if the service can do useful work.

    PHASE-3B: orchestrator_pool dependency removed. MarketOrchestrator +
    ComputeScheduler are gone; RunScheduler + RunDispatcher own scheduling
    now. This endpoint still returns the same wire schema (HealthPayload)
    for backward compat with external scrapes, but the compute_scheduler_alive
    / orchestrator_pool_alive / distributor_alive fields are permanently
    None. A follow-up (Phase 3c) will replace them with run_scheduler_alive
    + run_dispatcher_alive fields that reflect the actual subsystem state.

    Current readiness signals:
      - Cache has at least one snapshot
      - Redis reachable (if configured)
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

    # Phase 9 A2: stale tickers — informational only.
    stale = cache.all_stale_snapshots()

    payload = HealthPayload(
        status="ready" if not issues else "not_ready",
        registry_size=n_snapshots,   # legacy wire field
        cache_size=n_snapshots,
        # Legacy fields — subsystems deleted in Phase 3b. Kept as None
        # so the wire schema stays backward-compatible.
        compute_scheduler_alive=None,
        distributor_alive=None,
        compute_last_completed_at=None,
        distributor_last_completed_at=None,
        redis_reachable=redis_reachable,
        orchestrator_pool_alive=None,
        orchestrator_pool_last_tick_at=None,
        stale_tickers_count=len(stale),
        issues=issues,
    )

    if issues:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "issues": issues},
        )
    return payload