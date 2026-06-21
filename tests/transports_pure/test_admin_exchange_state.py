"""Tests for /v1/admin/exchange-state routes.

DB-free: dependency overrides replace get_db_engine. The
ExchangeStateRepository is monkeypatched on the route module so
we drive the route logic with hand-crafted ExchangeState dataclass
instances rather than a live Postgres.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.persistence.exchange_state_repo import ExchangeState
from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    get_cache, get_compute_scheduler, get_config, get_db_engine,
    get_distributor, get_etl_executor, get_redis_queue, )


# ---------- shared fixtures ---------- #


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setenv("TRIP_EMA_ADMIN_TOKEN", "test-token-xyz")
    return "test-token-xyz"


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _make_config():
    cfg = MagicMock()
    cfg.http.admin_token_env = "TRIP_EMA_ADMIN_TOKEN"
    return cfg


def _make_client(*, db_engine=None):
    app = create_app()
    app.dependency_overrides[get_config] = lambda: _make_config()
    app.dependency_overrides[get_db_engine] = lambda: (db_engine or MagicMock())
    app.dependency_overrides[get_etl_executor] = lambda: MagicMock()
    # Stubs for unrelated deps so DI resolves cleanly:
    app.dependency_overrides[get_cache] = lambda: MagicMock()
    app.dependency_overrides[get_compute_scheduler] = lambda: MagicMock()
    app.dependency_overrides[get_distributor] = lambda: None
    app.dependency_overrides[get_redis_queue] = lambda: None
    return TestClient(app)


def _make_state(exchange: str, **overrides) -> ExchangeState:
    """Build a canonical-shape ExchangeState for tests. All fields
    default to None / a fixed timestamp so individual tests only
    have to pass what they care about."""
    base = dict(
        exchange=exchange,
        initial_load_started_at=None,
        initial_load_completed_at=None,
        last_eod_run_at=None,
        last_eod_outcome=None,
        last_eod_data_date=None,
        last_eod_run_id=None,
        last_intraday_run_at=None,
        last_listings_sync_at=None,
        last_listings_run_id=None,
        last_tier_recompute_at=None,
        updated_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return ExchangeState(**base)


# ---------- GET /v1/admin/exchange-state (list) ---------- #


class TestExchangeStateList:

    def test_empty_list(self, auth_headers, monkeypatch):
        import trip_ema.transports.http.routes.admin_exchange_state as mod

        class FakeRepo:
            def __init__(self, conn): pass
            def list_all(self): return []
        monkeypatch.setattr(mod, "ExchangeStateRepository", FakeRepo)

        client = _make_client()
        resp = client.get(
            "/v1/admin/exchange-state", headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["exchanges"] == []
        assert body["total"] == 0
        assert "snapshot_taken_at" in body

    def test_returns_multiple_rows(self, auth_headers, monkeypatch):
        import trip_ema.transports.http.routes.admin_exchange_state as mod

        nse = _make_state(
            "NSE",
            initial_load_started_at=datetime(2026, 5, 18, 17, 22, 37, tzinfo=timezone.utc),
            initial_load_completed_at=datetime(2026, 5, 18, 20, 39, 23, tzinfo=timezone.utc),
            last_eod_run_at=datetime(2026, 5, 19, 19, 31, 21, tzinfo=timezone.utc),
            last_eod_outcome="success",
            last_eod_data_date=date(2026, 5, 19),
        )
        bse = _make_state(
            "BSE",
            last_eod_outcome="partial",
            last_eod_data_date=date(2026, 5, 18),
        )

        class FakeRepo:
            def __init__(self, conn): pass
            def list_all(self): return [bse, nse]  # arbitrary order from repo
        monkeypatch.setattr(mod, "ExchangeStateRepository", FakeRepo)

        client = _make_client()
        resp = client.get(
            "/v1/admin/exchange-state", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        codes = [e["exchange"] for e in body["exchanges"]]
        assert set(codes) == {"NSE", "BSE"}
        # Find NSE entry and check fields round-tripped:
        nse_payload = next(e for e in body["exchanges"] if e["exchange"] == "NSE")
        assert nse_payload["last_eod_outcome"] == "success"
        assert nse_payload["last_eod_data_date"] == "2026-05-19"
        assert nse_payload["initial_load_started_at"] is not None
        assert nse_payload["initial_load_completed_at"] is not None

    def test_requires_auth(self):
        import os
        os.environ.pop("TRIP_EMA_ADMIN_TOKEN", None)
        client = _make_client()
        resp = client.get("/v1/admin/exchange-state")
        assert resp.status_code in (401, 503)


# ---------- GET /v1/admin/exchange-state/{exchange} ---------- #


class TestExchangeStateOne:

    def test_returns_one_row(self, auth_headers, monkeypatch):
        import trip_ema.transports.http.routes.admin_exchange_state as mod

        nse = _make_state(
            "NSE",
            last_eod_run_at=datetime(2026, 5, 19, 19, 31, 21, tzinfo=timezone.utc),
            last_eod_outcome="success",
            last_eod_data_date=date(2026, 5, 19),
            last_eod_run_id=11,
        )

        class FakeRepo:
            def __init__(self, conn): pass
            def get(self, ex):
                return nse if ex == "NSE" else None
        monkeypatch.setattr(mod, "ExchangeStateRepository", FakeRepo)

        client = _make_client()
        resp = client.get(
            "/v1/admin/exchange-state/NSE", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["exchange"] == "NSE"
        assert body["last_eod_outcome"] == "success"
        assert body["last_eod_data_date"] == "2026-05-19"
        assert body["last_eod_run_id"] == 11

    def test_404_when_exchange_absent(self, auth_headers, monkeypatch):
        import trip_ema.transports.http.routes.admin_exchange_state as mod

        class FakeRepo:
            def __init__(self, conn): pass
            def get(self, ex): return None
        monkeypatch.setattr(mod, "ExchangeStateRepository", FakeRepo)

        client = _make_client()
        resp = client.get(
            "/v1/admin/exchange-state/BSE", headers=auth_headers,
        )
        assert resp.status_code == 404
        # Detail must name the exchange and point at the list endpoint
        detail = resp.json()["detail"]
        assert "BSE" in detail
        assert "list" in detail.lower()

    def test_requires_auth(self):
        import os
        os.environ.pop("TRIP_EMA_ADMIN_TOKEN", None)
        client = _make_client()
        resp = client.get("/v1/admin/exchange-state/NSE")
        assert resp.status_code in (401, 503)


# ---------- payload schema sanity ---------- #


class TestPayloadShape:
    """Round-trip every nullable field to make sure the Pydantic
    model accepts None and serializes correctly."""

    def test_all_nulls_serializes(self, auth_headers, monkeypatch):
        import trip_ema.transports.http.routes.admin_exchange_state as mod

        bare = _make_state("LSE")

        class FakeRepo:
            def __init__(self, conn): pass
            def get(self, ex): return bare if ex == "LSE" else None
        monkeypatch.setattr(mod, "ExchangeStateRepository", FakeRepo)

        client = _make_client()
        resp = client.get(
            "/v1/admin/exchange-state/LSE", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        # Every datetime/date field should be present as None on the wire
        for field in (
            "initial_load_started_at",
            "initial_load_completed_at",
            "last_eod_run_at",
            "last_eod_outcome",
            "last_eod_data_date",
            "last_eod_run_id",
            "last_intraday_run_at",
            "last_listings_sync_at",
            "last_listings_run_id",
            "last_tier_recompute_at",
        ):
            assert body[field] is None, f"{field} should be None"
        # updated_at is non-null on every row
        assert body["updated_at"] is not None
