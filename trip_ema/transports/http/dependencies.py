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
from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import Engine

from ...cache import SignalCache
from ...config import Config
from ...registry import TickerRegistry
from ...scheduler import ComputeScheduler, SubscriberDistributor

# Phase 7: optional Redis queue. Imported under TYPE_CHECKING to avoid a
# hard dependency on `redis` at import time for code that doesn't use it.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ...notifications.redis_queue import RedisQueue


@dataclass
class AppState:
    """Container for all runtime singletons. Populated by app.py at startup,
    consumed by the route dependencies below.

    Held as a single instance on the FastAPI app's state attribute so it
    survives across requests but doesn't leak into route code directly.
    """
    config: Config
    db_engine: Engine
    registry: TickerRegistry
    cache: SignalCache
    compute_scheduler: ComputeScheduler
    distributor: Optional[SubscriberDistributor] = None
    redis_queue: "Optional[RedisQueue]" = None
    metrics_server: "Optional[object]" = None
    """Phase 10: handle to the Prometheus /metrics HTTP server (separate
    port from the main API). None when metrics are disabled via config."""
    etl_executor: "Optional[object]" = None
    """Phase 11 Slice 2: a ThreadPoolExecutor used by the admin ETL
    endpoints to fire long-running runner functions (refresh-listings,
    EoD, backfill, ...) without blocking the HTTP request. None when
    the ETL admin endpoints are disabled (e.g., in tests that don't
    need them)."""


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


def get_registry() -> TickerRegistry:
    return _require_state().registry


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
