"""Tests for POST /v1/admin/v1pipeline/industry-indicator-state.

DB-free: dependency overrides replace get_db_engine, get_etl_executor.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    get_cache, get_compute_scheduler, get_config, get_db_engine,
    get_distributor, get_etl_executor, get_redis_queue,
)


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


def _make_config():
    cfg = MagicMock()
    cfg.http.admin_token_env = "TRIP_EMA_ADMIN_TOKEN"
    cfg.etl.exchange = "NSE"
    cfg.market_hours = [type("MH", (), {"exchange": "NSE"})()]
    return cfg


def _make_client(*, db_engine=None, executor=None):
    app = create_app()
    app.dependency_overrides[get_config] = lambda: _make_config()
    app.dependency_overrides[get_db_engine] = (
        lambda: (db_engine or MagicMock())
    )
    app.dependency_overrides[get_etl_executor] = lambda: executor
    app.dependency_overrides[get_cache] = lambda: MagicMock()
    app.dependency_overrides[get_compute_scheduler] = lambda: MagicMock()
    app.dependency_overrides[get_distributor] = lambda: None
    app.dependency_overrides[get_redis_queue] = lambda: None
    return TestClient(app)


# ============================================================
# Per-industry + bulk modes
# ============================================================

class TestIndustryIndicatorStateEndpoint:

    def test_per_industry_submits_runner(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/v1pipeline/industry-indicator-state"
            "?industry=IT%20-%20Services",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == (
            "v1pipeline.industry-indicator-state"
        )
        assert body["run_id"] is None
        assert "IT - Services" in body["notes"]

        assert len(exec_.submitted) == 1
        fn_name, args, _ = exec_.submitted[0]
        assert fn_name == (
            "_run_industry_indicator_state_for_industry_safe"
        )
        # args: (db_engine, industry, timeframe, min_bars)
        assert args[1] == "IT - Services"
        assert args[2] == "Weekly"
        assert args[3] == 30

    def test_per_industry_with_min_bars_override(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/v1pipeline/industry-indicator-state"
            "?industry=X&min_bars=5",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        fn_name, args, _ = exec_.submitted[0]
        assert args[3] == 5

    def test_all_industries_submits_bulk(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/v1pipeline/industry-indicator-state"
            "?all_industries=true",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert "all active industries" in resp.json()["notes"]

        fn_name, args, _ = exec_.submitted[0]
        assert fn_name == (
            "_run_industry_indicator_state_for_all_safe"
        )
        # args: (db_engine, timeframe, min_bars)
        assert args[1] == "Weekly"
        assert args[2] == 30


# ============================================================
# Validation errors
# ============================================================

class TestIndustryIndicatorStateValidation:

    def test_rejects_neither_industry_nor_all(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/industry-indicator-state",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_rejects_both_industry_and_all(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/industry-indicator-state"
            "?industry=X&all_industries=true",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_rejects_min_bars_zero(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/industry-indicator-state"
            "?industry=X&min_bars=0",
            headers=auth_headers,
        )
        # FastAPI Query validation rejects ge=1 violations as 422
        assert resp.status_code in (400, 422)


# ============================================================
# Auth + 503
# ============================================================

class TestIndustryIndicatorStateAuth:

    def test_503_when_executor_disabled(self, auth_headers):
        client = _make_client(executor=None)
        resp = client.post(
            "/v1/admin/v1pipeline/industry-indicator-state"
            "?industry=X",
            headers=auth_headers,
        )
        assert resp.status_code == 503

    def test_auth_required(self, admin_token):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/industry-indicator-state"
            "?industry=X",
        )
        assert resp.status_code in (401, 403)
