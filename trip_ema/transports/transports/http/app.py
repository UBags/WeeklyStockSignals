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
from typing import Callable

from fastapi import FastAPI

from .routes import admin, compute, health, signals

logger = logging.getLogger(__name__)


def create_app(shutdown_callback: Callable[[], None] | None = None) -> FastAPI:
    """Build the FastAPI app with all routers mounted.

    Args:
        shutdown_callback: Optional zero-arg callable invoked during ASGI
            lifespan shutdown. Use to stop schedulers and dispose of the
            DB engine. Must be safe to call once; uvicorn guarantees a
            single shutdown invocation per process.

    Does not initialize runtime state — `app.py` (the entry point) calls
    `dependencies.set_state()` after building the app to wire things up.
    """
    if shutdown_callback is not None:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            # Startup is a no-op — state wiring happens in app.py before
            # this lifespan begins, because we need the state available
            # during the boot sequence (priming the cache, etc.) BEFORE
            # uvicorn starts accepting requests. Doing startup in the
            # lifespan would create a chicken-and-egg with set_state().
            yield
            # Shutdown — uvicorn calls this on SIGTERM/SIGINT (it owns
            # the signal handlers when running via `uvicorn.run`).
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
    return app
