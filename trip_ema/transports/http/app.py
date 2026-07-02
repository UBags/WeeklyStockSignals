"""FastAPI app factory.

`create_app()` returns a configured FastAPI app with all routers mounted.
The app does NOT own the runtime state (registry, cache, schedulers) —
those are wired separately via `dependencies.set_state()`. This separation
lets tests build an app, then inject fake state without spinning up the
real entry point.

The optional `shutdown_callback` parameter participates in FastAPI's
ASGI-lifespan protocol. uvicorn invokes the lifespan on graceful
shutdown (SIGTERM in uvicorn's own handlers), giving us a hook to
stop background threads BEFORE the process exits. Without this hook
the schedulers' daemon threads would be killed mid-cycle, losing the
last bit of work in flight. Tests that don't need the lifespan can
just call `create_app()` with no argument — that path skips the
context manager entirely.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import Callable

from fastapi import FastAPI

# PHASE-3B: admin_orchestrator + pulse route imports removed (modules deleted).
from .routes import (
    admin, admin_etl, admin_exchange_state, admin_exchanges, admin_health,
    admin_notifications, admin_signals, admin_subscribers,
    admin_v1pipeline,
    compute, fundamentals, health, signals,
)

logger = logging.getLogger(__name__)


def create_app(
    shutdown_callback: Callable[[], None] | None = None,
    startup_callback: Callable[[], None] | None = None,
) -> FastAPI:
    """Build the FastAPI app with all routers mounted.

    Args:
        shutdown_callback: Optional zero-arg callable invoked during ASGI
            lifespan shutdown. Use to stop schedulers and dispose of the
            DB engine. Must be safe to call once; uvicorn guarantees a
            single shutdown invocation per process.
        startup_callback: Optional zero-arg callable invoked during ASGI
            lifespan startup, AFTER set_state() has been called by the
            entry point. Phase 14 uses this for two things: binding the
            PulseState to the asyncio event loop (so SSE subscribers can
            be fanned out from the orchestrator's worker thread), and
            starting the OrchestratorPool's per-market threads. Failures
            here do not abort startup — they are logged and the app
            continues with degraded functionality (pulse stream and
            orchestrator endpoints will return 503).

    Does not initialize runtime state — `app.py` (the entry point) calls
    `dependencies.set_state()` after building the app to wire things up.
    """
    needs_lifespan = (
        shutdown_callback is not None or startup_callback is not None
    )
    if needs_lifespan:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            # Startup — set_state() has already been called by app.py
            # before this lifespan begins. The startup_callback's job
            # is to bind the asyncio loop to PulseState (so it can
            # fan events out to SSE subscribers) and start the
            # OrchestratorPool. We do these here, not earlier in
            # app.py, because they need the event loop to exist —
            # which it does at this point but not in app.py's
            # pre-uvicorn boot sequence.
            if startup_callback is not None:
                try:
                    startup_callback()
                except Exception:
                    logger.exception(
                        "Error during ASGI lifespan startup; continuing "
                        "with degraded functionality",
                    )
            yield
            # Shutdown — uvicorn calls this on SIGTERM/SIGINT (it owns
            # the signal handlers when running via `uvicorn.run`).
            if shutdown_callback is not None:
                try:
                    shutdown_callback()
                except Exception:
                    logger.exception("Error during ASGI lifespan shutdown")

        app = FastAPI(
            title="trip_ema",
            description="Triple-EMA signal computation and notification service",
            version="0.1.0",
            lifespan=lifespan,
        )
    else:
        app = FastAPI(
            title="trip_ema",
            description="Triple-EMA signal computation and notification service",
            version="0.1.0",
        )

    app.include_router(health.router)
    app.include_router(signals.router)
    app.include_router(compute.router)
    app.include_router(admin.router)
    app.include_router(admin_etl.router)
    app.include_router(admin_exchange_state.router)
    app.include_router(admin_exchanges.router)
    app.include_router(admin_signals.router)
    app.include_router(admin_subscribers.router)
    app.include_router(admin_notifications.router)
    app.include_router(admin_health.router)
    # PHASE-3B: pulse + admin_orchestrator router registrations removed;
    # both routes were bound to the deleted MarketOrchestrator/PulseState.
    # Phase 1 fundamentals — industry classification + overrides + run
    # status. Reuses AppState.etl_executor for fire-and-forget refresh.
    app.include_router(fundamentals.router)
    # V1 clean-pipeline admin endpoints (Steps 4–8). Reuses etl_executor.
    app.include_router(admin_v1pipeline.router)
    return app
