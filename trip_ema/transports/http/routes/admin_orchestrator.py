"""Admin endpoints for the orchestrator pool.

All routes are bearer-token-gated via `require_admin_token`, matching
the pattern of `admin_etl.py` and `admin_subscribers.py`.

GET  /v1/admin/orchestrator/status              — per-market status
POST /v1/admin/orchestrator/{exchange}/run-now  — force one market's pipeline
POST /v1/admin/orchestrator/run-now-all         — force every market's pipeline

The two POST endpoints are fire-and-forget: they submit the
`orchestrator.run_once()` call onto the ETL executor's thread pool
and return 202 Accepted immediately. The HTTP request returns within
milliseconds; the actual pipeline runs asynchronously. Clients that
need the result should poll `/v1/admin/orchestrator/status` or
subscribe to `/v1/pulse/stream`.

Why reuse the ETL executor rather than running synchronously: a
full fetch → compute → distribute pipeline can take 3-5 minutes (or
longer during a yfinance hiccup). Holding an HTTP worker thread for
that duration would (a) starve other requests under uvicorn's
small default worker pool, and (b) often exceed reverse-proxy
read timeouts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from ..dependencies import (
    get_etl_executor,
    get_orchestrator_pool,
    require_admin_token,
)
from ..orchestrator_schemas import (
    OrchestratorMarketStatusPayload,
    OrchestratorRunNowAcceptedPayload,
    OrchestratorStatusPayload,
    OrchestratorStepStatusPayload,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/admin/orchestrator",
    tags=["admin", "orchestrator"],
    dependencies=[Depends(require_admin_token)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_pool(pool):
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Orchestrator is not running. Set "
                "`scheduling.orchestrator.enabled=true` in config."
            ),
        )


def _require_executor(executor):
    if executor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Orchestrator run-now requires the ETL executor, which is "
                "not configured. Set up `etl_executor` in AppState."
            ),
        )


def _step_to_payload(step) -> Optional[OrchestratorStepStatusPayload]:
    if step is None:
        return None
    return OrchestratorStepStatusPayload(
        succeeded=step.succeeded,
        skipped_reason=step.skipped_reason,
        error_message=step.error_message,
        duration_seconds=step.duration_seconds,
    )


def _orch_to_payload(orch) -> OrchestratorMarketStatusPayload:
    summary = orch.last_run_summary
    if summary is None:
        return OrchestratorMarketStatusPayload(
            exchange=orch.exchange,
            is_alive=orch.is_alive(),
            is_unhealthy=orch.is_unhealthy(),
        )
    return OrchestratorMarketStatusPayload(
        exchange=orch.exchange,
        is_alive=orch.is_alive(),
        is_unhealthy=orch.is_unhealthy(),
        last_run_started_at=summary.started_at,
        last_run_finished_at=summary.finished_at,
        last_run_correlation_id=summary.correlation_id or None,
        last_run_was_retry=summary.is_retry,
        last_run_retry_scheduled=summary.retry_scheduled,
        fetch=_step_to_payload(summary.fetch),
        compute=_step_to_payload(summary.compute),
        distribute=_step_to_payload(summary.distribute),
    )


def _run_once_safe(orch) -> None:
    """Run one orchestrator pipeline pass, catching all exceptions.

    Runs on the ETL executor's thread. `run_once()` itself swallows
    per-step exceptions, but we belt-and-braces here too — uncaught
    exceptions in an executor task become "loud" tracebacks in the
    server log without any operator-friendly context."""
    try:
        orch.run_once()
    except Exception:
        logger.exception(
            "Orchestrator[%s] run_once raised unexpectedly via admin endpoint",
            orch.exchange,
        )


# ---------------------------------------------------------------------------
# GET /v1/admin/orchestrator/status
# ---------------------------------------------------------------------------


@router.get("/status", response_model=OrchestratorStatusPayload)
def admin_orchestrator_status(
    pool = Depends(get_orchestrator_pool),
) -> OrchestratorStatusPayload:
    """Return status for every managed market.

    Includes pool-level health (`any_unhealthy`) and the last run
    summary per market. Use this for the operator console's
    orchestrator dashboard and for scripted health checks beyond
    /readyz's coarse boolean."""
    _require_pool(pool)
    markets = [_orch_to_payload(o) for o in pool.all()]
    return OrchestratorStatusPayload(
        pool_size=len(pool),
        any_unhealthy=pool.any_unhealthy(),
        markets=markets,
    )


# ---------------------------------------------------------------------------
# POST /v1/admin/orchestrator/{exchange}/run-now
# ---------------------------------------------------------------------------


@router.post(
    "/{exchange}/run-now",
    response_model=OrchestratorRunNowAcceptedPayload,
    status_code=status.HTTP_202_ACCEPTED,
)
def admin_orchestrator_run_now(
    exchange: str,
    pool = Depends(get_orchestrator_pool),
    executor = Depends(get_etl_executor),
) -> OrchestratorRunNowAcceptedPayload:
    """Force one market's full pipeline (fetch → compute → distribute)
    to fire now, outside the normal tick schedule.

    Returns 404 if the exchange isn't managed by the pool. Returns 202
    on acceptance — the pipeline runs asynchronously; poll /status
    or subscribe to /v1/pulse/stream for completion."""
    _require_pool(pool)
    _require_executor(executor)

    orch = pool.get(exchange)
    if orch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Exchange {exchange!r} is not managed by the orchestrator "
                f"pool. Managed exchanges: {pool.exchanges()}"
            ),
        )

    logger.info(
        "Admin: orchestrator run-now requested for %s", exchange,
    )
    executor.submit(_run_once_safe, orch)
    return OrchestratorRunNowAcceptedPayload(
        action="run-now",
        exchanges=[exchange],
        accepted_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# POST /v1/admin/orchestrator/run-now-all
# ---------------------------------------------------------------------------


@router.post(
    "/run-now-all",
    response_model=OrchestratorRunNowAcceptedPayload,
    status_code=status.HTTP_202_ACCEPTED,
)
def admin_orchestrator_run_now_all(
    pool = Depends(get_orchestrator_pool),
    executor = Depends(get_etl_executor),
) -> OrchestratorRunNowAcceptedPayload:
    """Force every market's pipeline to fire now.

    Each market's run_once() is submitted as a separate task to the
    ETL executor. The orchestrators run concurrently (subject to the
    executor's max_workers). Within each orchestrator, the
    reentrancy lock still applies — if a market is already mid-tick,
    its forced run is dropped with `skipped_due_to_concurrent_run`."""
    _require_pool(pool)
    _require_executor(executor)

    orchestrators = pool.all()
    logger.info(
        "Admin: orchestrator run-now-all requested for %d markets: %s",
        len(orchestrators), [o.exchange for o in orchestrators],
    )
    for orch in orchestrators:
        executor.submit(_run_once_safe, orch)

    return OrchestratorRunNowAcceptedPayload(
        action="run-now-all",
        exchanges=[o.exchange for o in orchestrators],
        accepted_at=datetime.now(timezone.utc),
    )
