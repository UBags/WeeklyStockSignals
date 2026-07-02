"""Tests for /v1/admin/v1pipeline/* routes.

DB-free: dependency overrides replace get_db_engine, get_config,
get_etl_executor. Verifies:
  - Single endpoint dispatches the right runner based on ticker_id
    vs exchange query param.
  - 400 when neither or both ticker_id and exchange are provided.
  - 400 when exchange is unknown/unconfigured.
  - 503 when the etl_executor is None.
  - Auth gate works (401/403 per the require_admin_token contract).

Pattern matches the existing admin_etl route tests.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    # === STEP 14B-FIXUP5-import: scheduler/distributor getters stripped ===
    get_v1_cache, get_config, get_db_engine,
    get_etl_executor, get_redis_queue,
)


# ---------- shared fixtures ---------- #


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setenv("TRIP_EMA_ADMIN_TOKEN", "test-token-xyz")
    return "test-token-xyz"


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


class FakeExecutor:
    """Captures submitted callables without running them."""

    def __init__(self):
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn.__name__, args, kwargs))
        return MagicMock()


def _make_config(*, market_hours_exchanges=("NSE",)):
    cfg = MagicMock()
    cfg.http.admin_token_env = "TRIP_EMA_ADMIN_TOKEN"
    cfg.etl.exchange = "NSE"
    cfg.market_hours = [
        type("MH", (), {"exchange": ex})()
        for ex in market_hours_exchanges
    ]
    return cfg


def _make_client(*, config=None, db_engine=None, executor=None):
    app = create_app()
    app.dependency_overrides[get_config] = (
        lambda: (config or _make_config())
    )
    app.dependency_overrides[get_db_engine] = (
        lambda: (db_engine or MagicMock())
    )
    app.dependency_overrides[get_etl_executor] = lambda: executor
    app.dependency_overrides[get_v1_cache] = lambda: MagicMock()
    # === STEP 14B-FIXUP5-overrides: scheduler/distributor overrides removed ===
    app.dependency_overrides[get_redis_queue] = lambda: None
    return TestClient(app)


# ============================================================
# POST /v1/admin/v1pipeline/stock-indicator-state
# ============================================================


class TestStockIndicatorStateEndpoint:

    def test_per_ticker_submits_runner(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-state"
            "?ticker_id=42&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == "v1pipeline.stock-indicator-state"
        assert body["run_id"] is None
        assert "ticker_id=42" in body["notes"]

        # One callable submitted: the per-ticker safe wrapper.
        assert len(exec_.submitted) == 1
        fn_name, args, _ = exec_.submitted[0]
        assert fn_name == "_run_stock_state_for_ticker_safe"
        # args: (db_engine, ticker_id, timeframe)
        assert args[1] == 42
        assert args[2] == "1W"

    def test_per_exchange_submits_runner(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-state"
            "?exchange=NSE&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "exchange=NSE" in body["notes"]
        assert "bulk" in body["notes"]

        assert len(exec_.submitted) == 1
        fn_name, args, _ = exec_.submitted[0]
        assert fn_name == "_run_stock_state_for_exchange_safe"
        # args: (db_engine, exchange, timeframe)
        assert args[1] == "NSE"
        assert args[2] == "1W"

    def test_rejects_neither_ticker_nor_exchange(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-state?timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "ticker_id" in resp.text or "exchange" in resp.text

    def test_rejects_both_ticker_and_exchange(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-state"
            "?ticker_id=1&exchange=NSE&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Exactly one" in resp.text or "exchange" in resp.text

    def test_rejects_unknown_exchange(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-state"
            "?exchange=XYZ&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_503_when_executor_disabled(self, auth_headers):
        # executor=None → service unavailable
        client = _make_client(executor=None)
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-state"
            "?ticker_id=42&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 503
        assert "disabled" in resp.text.lower() or "executor" in resp.text.lower()

    def test_auth_required(self, admin_token):
        # No Authorization header — should fail auth gate
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-state"
            "?ticker_id=42&timeframe=1W",
        )
        # Either 401 or 403 depending on require_admin_token's
        # behavior; either is correct.
        assert resp.status_code in (401, 403)
