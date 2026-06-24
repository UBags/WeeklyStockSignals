"""FastAPI dependencies.

Routes receive their collaborators by `Depends(...)`. The dependencies
themselves read from a module-level container that the entry point
populates at startup. Tests override these via FastAPI's `app.dependency_overrides`.

The container pattern is intentional: we want a single place where all
runtime state lives, but we don't want routes (or tests) to reach into
that container directly. Going through `Depends()` keeps the wiring
visible at the route signature level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import Engine

from ...cache import SignalCache
from ...config import Config
from ...scheduler import ComputeScheduler, SubscriberDistributor

# Phase 7: optional Redis queue. Imported under TYPE_CHECKING to avoid a
# hard dependency on `redis` at import time for code that doesn't use it.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from ...notifications.redis_queue import RedisQueue
    from ...scheduler import OrchestratorPool
    from ..http.pulse_state import PulseState


@dataclass
class AppState:
    """Container for all runtime singletons. Populated by app.py at startup,
    consumed by the route dependencies below.

    Held as a single instance on the FastAPI app's state attribute so it
    survives across requests but doesn't leak into route code directly.
    """
    config: Config
    db_engine: Engine
    cache: SignalCache
    compute_scheduler: ComputeScheduler
    distributor: Optional[SubscriberDistributor] = None
    redis_queue: "Optional[RedisQueue]" = None
    metrics_server: Optional[Any] = None
    """Phase 10: handle to the Prometheus /metrics HTTP server (separate
    port from the main API). None when metrics are disabled via config."""
    etl_executor: "Optional[ThreadPoolExecutor]" = None
    """Phase 11 Slice 2: a ThreadPoolExecutor used by the admin ETL
    endpoints to fire long-running runner functions (refresh-listings,
    EoD, backfill, ...) without blocking the HTTP request. None when
    the ETL admin endpoints are disabled (e.g., in tests that don't
    need them)."""

    orchestrator_pool: "Optional[OrchestratorPool]" = None
    """Phase 14: the OrchestratorPool managing per-market MarketOrchestrators.
    None when the orchestrator is disabled
    (`scheduling.orchestrator.enabled=False`); in that case the
    admin orchestrator endpoints return 503 and the legacy
    ComputeScheduler thread remains in charge. Typed as Optional[object]
    to avoid forward-reference cycles with the scheduler package."""

    pulse_state: "Optional[PulseState]" = None
    """Phase 14: the in-memory PulseState pub/sub for GUI live updates.
    None when no orchestrator is running — pulse is only meaningful
    when there's an orchestrator producing events.
    Typed via TYPE_CHECKING import for the same reason as orchestrator_pool."""


# Module-level singleton — set by app.py's create_app(), read by deps.
# Tests don't touch this directly; they use app.dependency_overrides.
_state: AppState | None = None


def set_state(state: AppState) -> None:
    """Wire the application state. Called once at startup."""
    global _state
    _state = state


def clear_state() -> None:
    """Clear the state — used in tests to ensure isolation between runs."""
    global _state
    _state = None


def _require_state() -> AppState:
    if _state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Application not yet initialized",
        )
    return _state


# ---------- Public dependency callables ---------- #


def get_config() -> Config:
    return _require_state().config


def get_db_engine() -> Engine:
    return _require_state().db_engine


def get_cache() -> SignalCache:
    return _require_state().cache


def get_compute_scheduler() -> ComputeScheduler:
    return _require_state().compute_scheduler


def get_distributor() -> SubscriberDistributor | None:
    return _require_state().distributor


def get_redis_queue():
    """May be None if Redis isn't configured."""
    return _require_state().redis_queue


# ---------- Admin auth ---------- #


def require_admin_token(
    authorization: Annotated[str | None, Header()] = None,
    config: Config = Depends(get_config),
) -> None:
    """Bearer-token gate for admin endpoints.

    Token is read from the env var named by `http.admin_token_env`. If the
    env var is unset, admin endpoints are completely disabled (any request
    returns 503). If the env var is set, the header must match exactly.
    """
    import os

    token_env = getattr(config.http, "admin_token_env", None) or "TRIP_EMA_ADMIN_TOKEN"
    expected = os.environ.get(token_env)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Admin endpoints disabled: {token_env} not set",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    presented = authorization[len("Bearer "):]
    if presented != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin token",
        )


def get_etl_executor():
    """Return the AppState's ETL ThreadPoolExecutor. Returns None when
    the ETL admin endpoints are disabled — the route handlers then
    return 503. Kept as Optional rather than required because tests
    and minimal deployments don't need it."""
    return _require_state().etl_executor


def get_orchestrator_pool():
    """Return the AppState's OrchestratorPool, or None.

    Returns None in two cases:
      1. Orchestrator disabled in config (orchestrator.enabled=false) —
         the AppState field is set but None.
      2. Application not yet initialized (e.g., in HTTP tests that
         build a minimal app without calling set_state()). Unlike
         most getters, this one does NOT raise 503 when state is
         missing — /readyz uses it and must not be blocked by an
         uninitialized state in tests.

    The admin orchestrator endpoints check `pool is None` themselves
    and return 503 with a clearer "orchestrator not running" message;
    the snapshot/stream pulse endpoints do the same for pulse_state."""
    if _state is None:
        return None
    return _state.orchestrator_pool


def get_pulse_state():
    """Return the AppState's PulseState, or None.

    Returns None in two cases:
      1. Orchestrator disabled in config — the AppState field is None.
      2. Application not yet initialized. Mirrors `get_orchestrator_pool`
         in tolerating missing state rather than 503-ing; the
         /v1/pulse and /v1/pulse/stream endpoints check `pulse_state
         is None` and return 503 with a clearer message in that case."""
    if _state is None:
        return None
    return _state.pulse_state
