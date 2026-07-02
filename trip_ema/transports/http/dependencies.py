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
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import Engine

# === STEP 14B-FIXUP1-deps-import: CurrentSignalsCache added ===
from ...cache import SignalCache
from ...cache.current_signals_cache import CurrentSignalsCache
from ...config import Config
# === STEP 14B-E3-import: ComputeScheduler + SubscriberDistributor imports stripped ===

# Phase 7: optional Redis queue. Imported under TYPE_CHECKING to avoid a
# hard dependency on `redis` at import time for code that doesn't use it.
from typing import TYPE_CHECKING
# PHASE-3B: OrchestratorPool + PulseState TYPE_CHECKING imports removed
# (both classes deleted alongside their modules).
if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from ...notifications.redis_queue import RedisQueue


@dataclass
class AppState:
    """Container for all runtime singletons. Populated by app.py at startup,
    consumed by the route dependencies below.

    Held as a single instance on the FastAPI app's state attribute so it
    survives across requests but doesn't leak into route code directly.
    """
    # === STEP 14B-E3-fields: compute_scheduler + distributor fields removed ===
    # === STEP 14B-FIXUP1-deps-appstate: v1_cache field added ===
    config: Config
    db_engine: Engine
    cache: SignalCache
    v1_cache: CurrentSignalsCache | None = None
    redis_queue: RedisQueue | None = None
    metrics_server: Any | None = None
    """Phase 10: handle to the Prometheus /metrics HTTP server (separate
    port from the main API). None when metrics are disabled via config."""
    etl_executor: ThreadPoolExecutor | None = None
    """Phase 11 Slice 2: a ThreadPoolExecutor used by the admin ETL
    endpoints to fire long-running runner functions (refresh-listings,
    EoD, backfill, ...) without blocking the HTTP request. None when
    the ETL admin endpoints are disabled (e.g., in tests that don't
    need them)."""

    # PHASE-3B: orchestrator_pool + pulse_state fields deleted; the
    # underlying OrchestratorPool + PulseState classes are gone.

    # PHASE-1A: new batch-run dispatcher singletons.
    run_queue: object | None = None
    """Phase 1a: RunQueue (in-memory FIFO with per-exchange dedup).
    Typed as `object` to avoid a scheduler->transports import cycle;
    routes cast at use site."""

    run_dispatcher: object | None = None
    """Phase 1a: RunDispatcher (subprocess-launcher thread that drains
    run_queue). Owns the SIGTERM/SIGKILL path for force-reset."""

    run_scheduler: object | None = None
    """Phase 3a: RunScheduler (wall-clock tick planner thread).
    Constructed always; started only when
    config.scheduling.dispatcher_enabled is True. Typed as `object`
    to avoid a scheduler->transports import cycle."""


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


# PHASE-1A: DI accessors for the batch-run subsystem.
def get_run_queue():
    """Return the RunQueue singleton, or 503 if Phase 1a subsystem
    is not initialized. Route handlers depend on this."""
    s = _require_state()
    if s.run_queue is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="run_queue not initialized",
        )
    return s.run_queue


def get_run_dispatcher():
    """Return the RunDispatcher singleton, or 503 if Phase 1a
    subsystem is not initialized."""
    s = _require_state()
    if s.run_dispatcher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="run_dispatcher not initialized",
        )
    return s.run_dispatcher


# PHASE-3C: None-tolerant health-probe getters. Unlike the "raise 503"
# accessors above, these MUST NOT raise for a missing subsystem — the
# health endpoints are supposed to observe subsystem state, not have
# their own responses hijacked by the DI layer when state is partially
# wired.
def get_run_scheduler_or_none():
    """Return AppState.run_scheduler, or None if state / subsystem
    is missing. Never raises. Used by /healthz + /readyz only."""
    if _state is None:
        return None
    return _state.run_scheduler


def get_run_dispatcher_or_none():
    """Return AppState.run_dispatcher, or None if state / subsystem
    is missing. Never raises. Used by /healthz + /readyz only."""
    if _state is None:
        return None
    return _state.run_dispatcher


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


# === STEP 14B-FIXUP1-deps-getter: get_v1_cache added ===
def get_v1_cache() -> CurrentSignalsCache:
    """Return the V1 CurrentSignalsCache, the read-through cache for
    `current_signals`. Used by /compute-now and other V1-path admin
    endpoints. Raises 503 if AppState was constructed without one
    (legacy test fixtures may do this)."""
    state = _require_state()
    if state.v1_cache is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="v1_cache not configured on AppState",
        )
    return state.v1_cache


# === STEP 14B-E3-getters: get_compute_scheduler + get_distributor removed ===

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


# PHASE-3B: get_orchestrator_pool() + get_pulse_state() DI accessors
# removed. Their consumer routes (admin_orchestrator.py, pulse.py) were
# deleted with them. admin_health._compute_scheduler_card now reads
# compute_runs directly instead of via orchestrator_pool.
