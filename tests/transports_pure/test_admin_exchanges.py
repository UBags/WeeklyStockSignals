"""Tests for /v1/admin/exchanges (Phase 14.6c).

DB-free — the route is pure config readout. Auth gate behavior is
covered by the shared admin-auth tests; here we focus on response
shape + ordering + edge cases.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    # === STEP 14B-FIXUP4-import: scheduler/distributor getters stripped ===
    get_cache, get_config, get_db_engine,
    get_etl_executor, get_redis_queue, )


# ---------- shared fixtures ---------- #


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setenv("TRIP_EMA_ADMIN_TOKEN", "test-token-xyz")
    return "test-token-xyz"


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _make_config(market_hours_exchanges=("NSE",)):
    cfg = MagicMock()
    cfg.http.admin_token_env = "TRIP_EMA_ADMIN_TOKEN"
    cfg.market_hours = [
        type("MH", (), {"exchange": ex})() for ex in market_hours_exchanges
    ]
    return cfg


def _make_client(*, config=None):
    app = create_app()
    app.dependency_overrides[get_config] = lambda: (config or _make_config())
    app.dependency_overrides[get_db_engine] = lambda: MagicMock()
    app.dependency_overrides[get_etl_executor] = lambda: MagicMock()
    app.dependency_overrides[get_cache] = lambda: MagicMock()
    # === STEP 14B-FIXUP4-overrides: scheduler/distributor overrides removed ===
    app.dependency_overrides[get_redis_queue] = lambda: None
    return TestClient(app)


# ---------- list endpoint ---------- #


class TestAdminExchangesList:

    def test_returns_configured_exchanges(self, auth_headers):
        cfg = _make_config(market_hours_exchanges=("NSE", "BSE", "NYSE"))
        client = _make_client(config=cfg)
        resp = client.get("/v1/admin/exchanges", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Alphabetic sort per route handler (deterministic dropdown order)
        assert body["exchanges"] == ["BSE", "NSE", "NYSE"]
        assert body["total"] == 3
        assert "snapshot_taken_at" in body

    def test_single_exchange(self, auth_headers):
        cfg = _make_config(market_hours_exchanges=("NSE",))
        client = _make_client(config=cfg)
        resp = client.get("/v1/admin/exchanges", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["exchanges"] == ["NSE"]
        assert body["total"] == 1

    def test_empty_market_hours_returns_empty_list(self, auth_headers):
        """An operator who forgot to configure market_hours: at all
        should see total=0 — the GUI can then surface a clear
        misconfiguration banner rather than crashing."""
        cfg = _make_config(market_hours_exchanges=())
        client = _make_client(config=cfg)
        resp = client.get("/v1/admin/exchanges", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["exchanges"] == []
        assert body["total"] == 0

    def test_ordering_is_stable_alphabetic(self, auth_headers):
        """Config-order shouldn't matter — the dropdown order should
        be deterministic regardless of how the operator arranged
        config.yaml."""
        cfg = _make_config(market_hours_exchanges=("NYSE", "BSE", "NSE"))
        client = _make_client(config=cfg)
        resp = client.get("/v1/admin/exchanges", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["exchanges"] == ["BSE", "NSE", "NYSE"]

    def test_requires_auth(self):
        import os
        os.environ.pop("TRIP_EMA_ADMIN_TOKEN", None)
        client = _make_client()
        resp = client.get("/v1/admin/exchanges")
        # Either 401 (auth header missing) or 503 (admin disabled) —
        # mirrors the pattern in the sibling route tests.
        assert resp.status_code in (401, 503)
