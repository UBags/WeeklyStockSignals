"""Tests for POST /v1/admin/v1pipeline/stock-indicator-enrichment.

DB-free: dependency overrides replace get_db_engine, get_config,
get_etl_executor. Mirrors test_admin_v1pipeline.py's structure for
the enrichment endpoint counterpart.
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
# POST /v1/admin/v1pipeline/stock-indicator-enrichment
# ============================================================


class TestStockIndicatorEnrichmentEndpoint:

    def test_per_ticker_submits_runner(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-enrichment"
            "?ticker_id=42&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == "v1pipeline.stock-indicator-enrichment"
        assert body["run_id"] is None
        assert "ticker_id=42" in body["notes"]

        assert len(exec_.submitted) == 1
        fn_name, args, _ = exec_.submitted[0]
        assert fn_name == "_run_stock_enrichment_for_ticker_safe"
        assert args[1] == 42
        assert args[2] == "1W"

    def test_per_exchange_submits_runner(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-enrichment"
            "?exchange=NSE&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "exchange=NSE" in body["notes"]
        assert "bulk" in body["notes"]

        assert len(exec_.submitted) == 1
        fn_name, args, _ = exec_.submitted[0]
        assert fn_name == "_run_stock_enrichment_for_exchange_safe"
        assert args[1] == "NSE"
        assert args[2] == "1W"

    def test_rejects_neither_ticker_nor_exchange(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-enrichment"
            "?timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert (
            "ticker_id" in resp.text or "exchange" in resp.text
        )

    def test_rejects_both_ticker_and_exchange(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-enrichment"
            "?ticker_id=1&exchange=NSE&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert (
            "Exactly one" in resp.text or "exchange" in resp.text
        )

    def test_rejects_unknown_exchange(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-enrichment"
            "?exchange=XYZ&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_503_when_executor_disabled(self, auth_headers):
        client = _make_client(executor=None)
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-enrichment"
            "?ticker_id=42&timeframe=1W",
            headers=auth_headers,
        )
        assert resp.status_code == 503
        assert (
            "disabled" in resp.text.lower()
            or "executor" in resp.text.lower()
        )

    def test_auth_required(self, admin_token):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/stock-indicator-enrichment"
            "?ticker_id=42&timeframe=1W",
        )
        assert resp.status_code in (401, 403)
