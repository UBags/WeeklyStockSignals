"""Tests for Phase 14 Step 6 — create_app wiring of the new routers
and the startup_callback parameter.

These tests do NOT exercise the real `trip_ema.app._build_app_state`
(which needs a Postgres DB and real config). They exercise the
`create_app` factory in `trip_ema.transports.http.app` directly with
fake AppState. The `_build_orchestrator_components` helper is tested
separately in `test_build_orchestrator_components.py`.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.transports.http.app import create_app


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


class TestRouterRegistration:
    def test_pulse_router_mounted(self):
        """GET /v1/pulse exists (returns 503 because state is empty)."""
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/v1/pulse")
        # 503 is the "no pulse_state configured" response; 404 would
        # mean the router isn't mounted at all.
        assert resp.status_code == 503

    def test_pulse_stream_router_mounted(self):
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/v1/pulse/stream")
        assert resp.status_code == 503

    def test_admin_orchestrator_status_router_mounted(self):
        """GET /v1/admin/orchestrator/status exists (returns 503 because
        admin token env unset)."""
        # Ensure no admin token in env, so we get the 'admin disabled'
        # 503 from require_admin_token rather than 401/403.
        prev = os.environ.pop("TRIP_EMA_ADMIN_TOKEN", None)
        try:
            app = create_app()
            with TestClient(app) as client:
                resp = client.get("/v1/admin/orchestrator/status")
            # Without admin token configured, the auth dep raises 503.
            # The point is the router is mounted (not 404).
            assert resp.status_code in (401, 503), (
                f"unexpected status {resp.status_code}; body={resp.text}"
            )
        finally:
            if prev is not None:
                os.environ["TRIP_EMA_ADMIN_TOKEN"] = prev

    def test_admin_orchestrator_run_now_router_mounted(self):
        prev = os.environ.pop("TRIP_EMA_ADMIN_TOKEN", None)
        try:
            app = create_app()
            with TestClient(app) as client:
                resp = client.post("/v1/admin/orchestrator/NSE/run-now")
            assert resp.status_code in (401, 503)
        finally:
            if prev is not None:
                os.environ["TRIP_EMA_ADMIN_TOKEN"] = prev

    def test_admin_orchestrator_run_now_all_router_mounted(self):
        prev = os.environ.pop("TRIP_EMA_ADMIN_TOKEN", None)
        try:
            app = create_app()
            with TestClient(app) as client:
                resp = client.post("/v1/admin/orchestrator/run-now-all")
            assert resp.status_code in (401, 503)
        finally:
            if prev is not None:
                os.environ["TRIP_EMA_ADMIN_TOKEN"] = prev

    def test_existing_routers_still_mounted(self):
        """Sanity: Step 6 must not have broken the existing router list.

        We can't easily hit /healthz here because it requires AppState
        to be set, and these tests intentionally don't set it. But we
        can introspect the app's routes directly to verify health is
        registered."""
        app = create_app()
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        # Pre-Phase-14 routes still present.
        assert "/healthz" in paths
        assert "/readyz" in paths
        # Phase 14 routes also present.
        assert "/v1/pulse" in paths
        assert "/v1/pulse/stream" in paths
        assert "/v1/admin/orchestrator/status" in paths


# ---------------------------------------------------------------------------
# Lifespan: startup_callback + shutdown_callback parameters
# ---------------------------------------------------------------------------


class TestLifespanCallbacks:
    def test_startup_callback_invoked_on_lifespan_entry(self):
        startup = MagicMock()
        app = create_app(startup_callback=startup)
        with TestClient(app):
            # Entering the TestClient context manager triggers the
            # ASGI lifespan startup.
            pass
        startup.assert_called_once()

    def test_shutdown_callback_invoked_on_lifespan_exit(self):
        shutdown = MagicMock()
        app = create_app(shutdown_callback=shutdown)
        with TestClient(app):
            pass
        shutdown.assert_called_once()

    def test_both_callbacks_invoked_in_order(self):
        order = []
        startup = lambda: order.append("startup")
        shutdown = lambda: order.append("shutdown")
        app = create_app(
            startup_callback=startup, shutdown_callback=shutdown,
        )
        with TestClient(app):
            pass
        assert order == ["startup", "shutdown"]

    def test_neither_callback_means_no_lifespan_setup(self):
        """When neither callback is given, the app shouldn't carry a
        lifespan handler at all — keeps the test path light for
        tests that don't need lifespan behaviour."""
        app = create_app()
        # Just verify it builds and serves *something* (our pulse
        # endpoint correctly returns 503 when pulse_state is unset,
        # which proves the router is reachable and not 404).
        with TestClient(app) as client:
            resp = client.get("/v1/pulse")
        assert resp.status_code == 503

    def test_startup_callback_exception_does_not_abort_lifespan(self):
        """A throwing startup_callback should be logged, not propagated.
        Otherwise a misconfigured orchestrator would block ALL requests."""
        def bad_startup():
            raise RuntimeError("simulated startup failure")
        app = create_app(startup_callback=bad_startup)
        # Should not raise — and the rest of the app should still work.
        with TestClient(app) as client:
            resp = client.get("/v1/pulse")
        assert resp.status_code == 503  # endpoint reachable, 503 b/c no state

    def test_shutdown_callback_exception_does_not_break_app(self):
        def bad_shutdown():
            raise RuntimeError("simulated shutdown failure")
        app = create_app(shutdown_callback=bad_shutdown)
        # The lifespan exit catches and logs.
        with TestClient(app) as client:
            resp = client.get("/v1/pulse")
        assert resp.status_code == 503
        # Exiting the context manager triggers shutdown; should not raise.


# ---------------------------------------------------------------------------
# Readyz integration with orchestrator_pool
# ---------------------------------------------------------------------------


class TestReadyzOrchestrator:
    """Readyz must surface orchestrator pool health.

    These tests use FastAPI's dependency_overrides to inject a mock
    pool, since wiring real state in via set_state() is heavier than
    these checks need."""

    def _ready_app_with_pool(self, pool):
        """Build an app with the minimal AppState overrides /readyz needs,
        plus the orchestrator_pool override."""
        from trip_ema.cache import SignalCache
        from trip_ema.domain import Timeframe
        from trip_ema.registry import TickerRegistry
        from trip_ema.transports.http.dependencies import (
            get_cache,
            get_compute_scheduler,
            get_distributor,
            get_orchestrator_pool,
            get_redis_queue,
            get_registry,
        )

        # Stub scheduler that reports never-started, never-completed
        # (healthy in /readyz's view).
        stub_scheduler = MagicMock()
        stub_scheduler.is_alive.return_value = False
        stub_scheduler.is_unhealthy.return_value = None
        stub_scheduler.last_run_completed_at = None

        # Cache + registry with at least one entry each so /readyz
        # doesn't fail on emptiness checks first.
        cache = SignalCache()
        registry = TickerRegistry.__new__(TickerRegistry)
        # Inject minimal state directly to avoid full init machinery.
        registry._engines = {("X", Timeframe.WEEKLY): MagicMock()}
        registry._params = MagicMock()
        # Stub __len__ to satisfy /readyz.
        type(registry).__len__ = lambda self: len(self._engines)
        # Populate cache too.
        cache._entries = {("X", Timeframe.WEEKLY): MagicMock()}
        type(cache).__len__ = lambda self: len(self._entries)
        cache.stale_signals = lambda: []

        app = create_app()
        app.dependency_overrides[get_cache] = lambda: cache
        app.dependency_overrides[get_registry] = lambda: registry
        app.dependency_overrides[get_compute_scheduler] = lambda: stub_scheduler
        app.dependency_overrides[get_distributor] = lambda: None
        app.dependency_overrides[get_redis_queue] = lambda: None
        app.dependency_overrides[get_orchestrator_pool] = lambda: pool
        return app

    def test_readyz_ok_when_pool_is_none(self):
        """Orchestrator disabled: pool is None, /readyz unaffected."""
        app = self._ready_app_with_pool(None)
        with TestClient(app) as client:
            resp = client.get("/readyz")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ready"

    def test_readyz_ok_when_pool_healthy(self):
        pool = MagicMock()
        pool.any_unhealthy.return_value = False
        app = self._ready_app_with_pool(pool)
        with TestClient(app) as client:
            resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_readyz_503_when_pool_unhealthy(self):
        pool = MagicMock()
        pool.any_unhealthy.return_value = True
        app = self._ready_app_with_pool(pool)
        with TestClient(app) as client:
            resp = client.get("/readyz")
        assert resp.status_code == 503
        # FastAPI's HTTPException(detail=...) returns {"detail": {...}}.
        body = resp.json()
        detail = body["detail"]
        assert detail["status"] == "not_ready"
        # Surface a clear error message about which subsystem failed.
        assert any(
            "orchestrator" in issue.lower() for issue in detail["issues"]
        ), detail


class TestDependencyGettersToleratesMissingState:
    """Regression coverage: get_orchestrator_pool and get_pulse_state
    must return None (not raise 503) when AppState hasn't been set.

    The original Step 6 patch made both go through `_require_state()`
    like every other getter — which broke the existing /readyz tests
    in tests/transports/http/test_health.py because those tests build
    a minimal app without calling set_state(). The fix: tolerate
    missing state in these two getters; the route handlers already
    know how to handle `None` and produce useful error messages."""

    def test_get_orchestrator_pool_returns_none_without_state(self):
        from trip_ema.transports.http.dependencies import (
            clear_state, get_orchestrator_pool,
        )
        clear_state()
        # Must not raise. Must return None.
        assert get_orchestrator_pool() is None

    def test_get_pulse_state_returns_none_without_state(self):
        from trip_ema.transports.http.dependencies import (
            clear_state, get_pulse_state,
        )
        clear_state()
        assert get_pulse_state() is None
