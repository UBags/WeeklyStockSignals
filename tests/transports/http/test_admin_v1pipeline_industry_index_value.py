"""Tests for POST /v1/admin/v1pipeline/industry-index-value.

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
    cfg.market_hours = [
        type("MH", (), {"exchange": "NSE"})(),
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
    app.dependency_overrides[get_cache] = lambda: MagicMock()
    app.dependency_overrides[get_compute_scheduler] = lambda: MagicMock()
    app.dependency_overrides[get_distributor] = lambda: None
    app.dependency_overrides[get_redis_queue] = lambda: None
    return TestClient(app)


# ============================================================
# Per-industry mode
# ============================================================

class TestIndustryIndexValueEndpoint:

    def test_per_industry_submits_runner(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/v1pipeline/industry-index-value"
            "?industry=IT%20-%20Services",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == "v1pipeline.industry-index-value"
        assert body["run_id"] is None
        assert "IT - Services" in body["notes"]

        assert len(exec_.submitted) == 1
        fn_name, args, _ = exec_.submitted[0]
        assert fn_name == "_run_industry_index_value_for_industry_safe"
        # args: (db_engine, industry, from_date, to_date, resume_run_id)
        assert args[1] == "IT - Services"
        assert args[2] is None
        assert args[3] is None
        assert args[4] is None

    def test_per_industry_with_date_range_and_resume(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/v1pipeline/industry-index-value"
            "?industry=X&from_date=2026-01-01&to_date=2026-06-01"
            "&resume_run_id=42",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert "from_date=2026-01-01" in resp.json()["notes"]
        assert "to_date=2026-06-01" in resp.json()["notes"]
        assert "resume_run_id=42" in resp.json()["notes"]

        fn_name, args, _ = exec_.submitted[0]
        assert fn_name == "_run_industry_index_value_for_industry_safe"
        from datetime import date
        assert args[2] == date(2026, 1, 1)
        assert args[3] == date(2026, 6, 1)
        assert args[4] == 42

    def test_all_industries_submits_bulk_runner(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/v1pipeline/industry-index-value"
            "?all_industries=true",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert "all active industries" in resp.json()["notes"]

        fn_name, args, _ = exec_.submitted[0]
        assert fn_name == "_run_industry_index_value_for_all_safe"
        # args: (db_engine, from_date, to_date)
        assert args[1] is None
        assert args[2] is None


# ============================================================
# Validation errors
# ============================================================

class TestIndustryIndexValueValidation:

    def test_rejects_neither_industry_nor_all(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/industry-index-value",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert (
            "industry" in resp.text or "all_industries" in resp.text
        )

    def test_rejects_both_industry_and_all(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/industry-index-value"
            "?industry=X&all_industries=true",
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_rejects_resume_with_all_industries(self, auth_headers):
        """resume_run_id only valid with single `industry`."""
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/industry-index-value"
            "?all_industries=true&resume_run_id=5",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "resume" in resp.text.lower()

    def test_rejects_inverted_date_range(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/industry-index-value"
            "?industry=X&from_date=2026-06-01&to_date=2026-01-01",
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "from_date" in resp.text or "to_date" in resp.text


# ============================================================
# Auth + 503
# ============================================================

class TestIndustryIndexValueAuth:

    def test_503_when_executor_disabled(self, auth_headers):
        client = _make_client(executor=None)
        resp = client.post(
            "/v1/admin/v1pipeline/industry-index-value"
            "?industry=X",
            headers=auth_headers,
        )
        assert resp.status_code == 503

    def test_auth_required(self, admin_token):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/v1pipeline/industry-index-value"
            "?industry=X",
        )
        assert resp.status_code in (401, 403)
