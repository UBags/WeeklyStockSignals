"""Tests for /v1/admin/orchestrator/* endpoints.

The admin endpoints fire pipeline runs onto a ThreadPoolExecutor, so
tests need to wait for the executor to finish. We use a tiny
ThreadPoolExecutor and `future.result()`-style polling. The
`require_admin_token` gate is exercised once; we don't re-test the
token mechanism itself (covered by tests/transports/http/test_admin.py).
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, UTC
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trip_ema.scheduler.market_orchestrator import (
    OrchestratorRunSummary,
)
from trip_ema.transports.http.dependencies import (
    get_config,
    get_etl_executor,
    get_orchestrator_pool,
)
from trip_ema.transports.http.routes import admin_orchestrator as admin_routes


UTC = UTC


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


_ADMIN_TOKEN_ENV = "TRIP_EMA_ADMIN_TOKEN"
_ADMIN_TOKEN_VALUE = "test-admin-token-step5"


@pytest.fixture(autouse=True)
def _set_admin_token():
    """Provide a known admin token for the duration of every test."""
    prev = os.environ.get(_ADMIN_TOKEN_ENV)
    os.environ[_ADMIN_TOKEN_ENV] = _ADMIN_TOKEN_VALUE
    yield
    if prev is None:
        os.environ.pop(_ADMIN_TOKEN_ENV, None)
    else:
        os.environ[_ADMIN_TOKEN_ENV] = prev


def _mock_config():
    """Minimal Config stand-in: only http.admin_token_env is accessed."""
    cfg = MagicMock()
    cfg.http.admin_token_env = _ADMIN_TOKEN_ENV
    return cfg


def _mock_orch(
    exchange: str,
    *,
    is_alive: bool = True,
    unhealthy: bool = False,
    last_run_summary: OrchestratorRunSummary | None = None,
) -> MagicMock:
    m = MagicMock()
    m.exchange = exchange
    m.is_alive.return_value = is_alive
    m.is_unhealthy.return_value = unhealthy
    m.last_run_summary = last_run_summary
    return m


def _mock_pool(orchestrators):
    m = MagicMock()
    by_ex = {o.exchange: o for o in orchestrators}
    m.exchanges.return_value = sorted(by_ex)
    m.all.return_value = sorted(orchestrators, key=lambda o: o.exchange)
    m.get.side_effect = by_ex.get
    m.__len__.return_value = len(orchestrators)
    m.any_unhealthy.return_value = any(
        o.is_unhealthy.return_value for o in orchestrators
    )
    return m


def _build_app(*, pool, executor=None):
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.dependency_overrides[get_config] = _mock_config
    app.dependency_overrides[get_orchestrator_pool] = lambda: pool
    app.dependency_overrides[get_etl_executor] = lambda: executor
    return app


def _auth():
    return {"Authorization": f"Bearer {_ADMIN_TOKEN_VALUE}"}


def _dummy_summary(exchange: str = "NSE") -> OrchestratorRunSummary:
    s = OrchestratorRunSummary(
        exchange=exchange,
        started_at=datetime(2026, 5, 19, 5, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 19, 5, 0, 1, tzinfo=UTC),
        correlation_id=f"cid-{exchange}",
    )
    s.fetch.succeeded = True
    s.fetch.duration_seconds = 0.5
    s.compute.succeeded = True
    s.compute.duration_seconds = 1.2
    s.distribute.succeeded = True
    s.distribute.duration_seconds = 0.3
    return s


# ---------------------------------------------------------------------------
# GET /v1/admin/orchestrator/status
# ---------------------------------------------------------------------------


class TestStatusEndpoint:
    def test_status_lists_all_markets(self):
        pool = _mock_pool([_mock_orch("NSE"), _mock_orch("BSE")])
        app = _build_app(pool=pool)
        with TestClient(app) as client:
            resp = client.get(
                "/v1/admin/orchestrator/status", headers=_auth(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pool_size"] == 2
        assert body["any_unhealthy"] is False
        ex_in_response = {m["exchange"] for m in body["markets"]}
        assert ex_in_response == {"NSE", "BSE"}

    def test_status_includes_last_run_summary(self):
        nse = _mock_orch("NSE", last_run_summary=_dummy_summary("NSE"))
        pool = _mock_pool([nse])
        app = _build_app(pool=pool)
        with TestClient(app) as client:
            resp = client.get(
                "/v1/admin/orchestrator/status", headers=_auth(),
            )
        body = resp.json()
        nse_market = body["markets"][0]
        assert nse_market["last_run_correlation_id"] == "cid-NSE"
        assert nse_market["fetch"]["succeeded"] is True
        assert nse_market["compute"]["succeeded"] is True
        assert nse_market["distribute"]["succeeded"] is True

    def test_status_market_without_run_has_null_summary_fields(self):
        # Orchestrator started but never ran a pipeline.
        pool = _mock_pool([_mock_orch("NSE")])
        app = _build_app(pool=pool)
        with TestClient(app) as client:
            resp = client.get(
                "/v1/admin/orchestrator/status", headers=_auth(),
            )
        nse_market = resp.json()["markets"][0]
        assert nse_market["last_run_started_at"] is None
        assert nse_market["fetch"] is None
        assert nse_market["compute"] is None
        assert nse_market["distribute"] is None

    def test_status_surfaces_pool_any_unhealthy(self):
        pool = _mock_pool([
            _mock_orch("NSE"), _mock_orch("BSE", unhealthy=True),
        ])
        app = _build_app(pool=pool)
        with TestClient(app) as client:
            resp = client.get(
                "/v1/admin/orchestrator/status", headers=_auth(),
            )
        body = resp.json()
        assert body["any_unhealthy"] is True

    def test_status_requires_admin_token(self):
        pool = _mock_pool([_mock_orch("NSE")])
        app = _build_app(pool=pool)
        with TestClient(app) as client:
            # No header.
            resp = client.get("/v1/admin/orchestrator/status")
        assert resp.status_code == 401

    def test_status_wrong_token_forbidden(self):
        pool = _mock_pool([_mock_orch("NSE")])
        app = _build_app(pool=pool)
        with TestClient(app) as client:
            resp = client.get(
                "/v1/admin/orchestrator/status",
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 403

    def test_status_503_when_pool_disabled(self):
        # pool=None simulates the case where orchestrator is disabled.
        app = _build_app(pool=None)
        with TestClient(app) as client:
            resp = client.get(
                "/v1/admin/orchestrator/status", headers=_auth(),
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /v1/admin/orchestrator/{exchange}/run-now
# ---------------------------------------------------------------------------


class TestRunNowOne:
    def test_run_now_returns_202(self):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            nse = _mock_orch("NSE")
            pool = _mock_pool([nse, _mock_orch("BSE")])
            app = _build_app(pool=pool, executor=executor)
            with TestClient(app) as client:
                resp = client.post(
                    "/v1/admin/orchestrator/NSE/run-now",
                    headers=_auth(),
                )
            assert resp.status_code == 202
            body = resp.json()
            assert body["action"] == "run-now"
            assert body["exchanges"] == ["NSE"]
        finally:
            executor.shutdown(wait=True)

    def test_run_now_actually_invokes_orchestrator_run_once(self):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            run_event = threading.Event()
            nse = _mock_orch("NSE")
            nse.run_once.side_effect = lambda: run_event.set()
            pool = _mock_pool([nse])
            app = _build_app(pool=pool, executor=executor)
            with TestClient(app) as client:
                client.post(
                    "/v1/admin/orchestrator/NSE/run-now",
                    headers=_auth(),
                )
            # Wait briefly for the executor task.
            assert run_event.wait(timeout=2.0), "run_once was not called"
            nse.run_once.assert_called_once()
        finally:
            executor.shutdown(wait=True)

    def test_run_now_unknown_exchange_returns_404(self):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            pool = _mock_pool([_mock_orch("NSE")])
            app = _build_app(pool=pool, executor=executor)
            with TestClient(app) as client:
                resp = client.post(
                    "/v1/admin/orchestrator/XYZ/run-now",
                    headers=_auth(),
                )
            assert resp.status_code == 404
            assert "XYZ" in resp.json()["detail"]
        finally:
            executor.shutdown(wait=True)

    def test_run_now_503_when_pool_disabled(self):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            app = _build_app(pool=None, executor=executor)
            with TestClient(app) as client:
                resp = client.post(
                    "/v1/admin/orchestrator/NSE/run-now",
                    headers=_auth(),
                )
            assert resp.status_code == 503
        finally:
            executor.shutdown(wait=True)

    def test_run_now_503_when_executor_disabled(self):
        pool = _mock_pool([_mock_orch("NSE")])
        app = _build_app(pool=pool, executor=None)
        with TestClient(app) as client:
            resp = client.post(
                "/v1/admin/orchestrator/NSE/run-now", headers=_auth(),
            )
        assert resp.status_code == 503
        assert "ETL executor" in resp.json()["detail"]

    def test_run_now_requires_admin_token(self):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            pool = _mock_pool([_mock_orch("NSE")])
            app = _build_app(pool=pool, executor=executor)
            with TestClient(app) as client:
                resp = client.post("/v1/admin/orchestrator/NSE/run-now")
            assert resp.status_code == 401
        finally:
            executor.shutdown(wait=True)

    def test_run_once_exception_does_not_break_endpoint(self):
        """If orchestrator.run_once raises, the executor task logs and
        swallows it. The HTTP response is still 202 — operator already
        got the acceptance ack."""
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            nse = _mock_orch("NSE")
            nse.run_once.side_effect = RuntimeError("boom")
            pool = _mock_pool([nse])
            app = _build_app(pool=pool, executor=executor)
            with TestClient(app) as client:
                resp = client.post(
                    "/v1/admin/orchestrator/NSE/run-now", headers=_auth(),
                )
            assert resp.status_code == 202
            # Give the executor time to process and log.
            time.sleep(0.1)
        finally:
            executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# POST /v1/admin/orchestrator/run-now-all
# ---------------------------------------------------------------------------


class TestRunNowAll:
    def test_run_now_all_returns_202_with_all_exchanges(self):
        executor = ThreadPoolExecutor(max_workers=4)
        try:
            pool = _mock_pool([
                _mock_orch("NSE"),
                _mock_orch("BSE"),
                _mock_orch("NYSE"),
            ])
            app = _build_app(pool=pool, executor=executor)
            with TestClient(app) as client:
                resp = client.post(
                    "/v1/admin/orchestrator/run-now-all",
                    headers=_auth(),
                )
            assert resp.status_code == 202
            body = resp.json()
            assert body["action"] == "run-now-all"
            assert set(body["exchanges"]) == {"NSE", "BSE", "NYSE"}
        finally:
            executor.shutdown(wait=True)

    def test_run_now_all_fires_every_orchestrator(self):
        executor = ThreadPoolExecutor(max_workers=4)
        try:
            done = threading.Event()
            run_count = [0]
            lock = threading.Lock()

            def mark_run():
                with lock:
                    run_count[0] += 1
                    if run_count[0] >= 3:
                        done.set()

            orchs = [_mock_orch(ex) for ex in ["NSE", "BSE", "NYSE"]]
            for o in orchs:
                o.run_once.side_effect = mark_run

            pool = _mock_pool(orchs)
            app = _build_app(pool=pool, executor=executor)
            with TestClient(app) as client:
                client.post(
                    "/v1/admin/orchestrator/run-now-all", headers=_auth(),
                )
            assert done.wait(timeout=2.0), (
                f"only {run_count[0]} of 3 orchestrators ran"
            )
            for o in orchs:
                o.run_once.assert_called_once()
        finally:
            executor.shutdown(wait=True)

    def test_run_now_all_empty_pool_is_ok(self):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            pool = _mock_pool([])
            app = _build_app(pool=pool, executor=executor)
            with TestClient(app) as client:
                resp = client.post(
                    "/v1/admin/orchestrator/run-now-all", headers=_auth(),
                )
            assert resp.status_code == 202
            assert resp.json()["exchanges"] == []
        finally:
            executor.shutdown(wait=True)

    def test_run_now_all_requires_admin_token(self):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            pool = _mock_pool([_mock_orch("NSE")])
            app = _build_app(pool=pool, executor=executor)
            with TestClient(app) as client:
                resp = client.post("/v1/admin/orchestrator/run-now-all")
            assert resp.status_code == 401
        finally:
            executor.shutdown(wait=True)
