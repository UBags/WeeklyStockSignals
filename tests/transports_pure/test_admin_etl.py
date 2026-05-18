"""Tests for the /v1/admin/etl/* routes.

DB-free: dependency overrides replace get_db_engine, get_config,
get_etl_executor. We verify:
  - The router fires the right runner function on the executor.
  - 503 when the executor is None.
  - Auth gate works (401/403/503 per env-var state).
  - GET /etl/status composes the response correctly when fed
    handcrafted row objects.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    get_cache, get_compute_scheduler, get_config, get_db_engine,
    get_distributor, get_etl_executor, get_redis_queue, get_registry,
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
    """Captures submitted callables without running them. Tests that
    care about the runner being called assert on `.submitted`."""

    def __init__(self):
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn.__name__, args, kwargs))
        # Return a no-op future-shaped object (the routes don't use it)
        return MagicMock()


def _make_config():
    cfg = MagicMock()
    cfg.http.admin_token_env = "TRIP_EMA_ADMIN_TOKEN"
    cfg.etl.exchange = "NSE"
    return cfg


def _make_client(*, config=None, db_engine=None, executor=None):
    """Build a TestClient with the standard overrides. Any arg=None
    means use a default MagicMock; pass an explicit value (including
    the literal None for executor, via FakeExecutor()) to override."""
    app = create_app()
    app.dependency_overrides[get_config] = lambda: (config or _make_config())
    app.dependency_overrides[get_db_engine] = lambda: (db_engine or MagicMock())
    app.dependency_overrides[get_etl_executor] = lambda: executor
    # Stub the rest so deps resolve cleanly
    app.dependency_overrides[get_registry] = lambda: MagicMock()
    app.dependency_overrides[get_cache] = lambda: MagicMock()
    app.dependency_overrides[get_compute_scheduler] = lambda: MagicMock()
    app.dependency_overrides[get_distributor] = lambda: None
    app.dependency_overrides[get_redis_queue] = lambda: None
    return TestClient(app)


# ---------- POST: refresh-listings ---------- #


class TestRefreshListings:

    def test_submits_runner_to_executor(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/etl/refresh-listings", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "refresh-listings"
        assert body["run_id"] is None  # async; run_id is unknown at accept time
        # The runner was scheduled
        assert len(exec_.submitted) == 1
        assert exec_.submitted[0][0] == "_run_refresh_listings_safe"

    def test_returns_503_without_executor(self, auth_headers):
        client = _make_client(executor=None)
        resp = client.post(
            "/v1/admin/etl/refresh-listings", headers=auth_headers,
        )
        assert resp.status_code == 503
        assert "disabled" in resp.json()["detail"].lower()

    def test_requires_auth(self):
        # No env var, no header → 503 (admin disabled)
        import os
        os.environ.pop("TRIP_EMA_ADMIN_TOKEN", None)
        client = _make_client(executor=FakeExecutor())
        resp = client.post("/v1/admin/etl/refresh-listings")
        assert resp.status_code in (401, 503)


# ---------- POST: run-eod ---------- #


class TestRunEod:

    def test_default_lookback(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post("/v1/admin/etl/run-eod", headers=auth_headers)
        assert resp.status_code == 200
        # Defaults: lookback_days=7
        _, args, _ = exec_.submitted[0]
        # args = (db_engine, config, lookback_days)
        assert args[2] == 7

    def test_custom_lookback(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/etl/run-eod?lookback_days=30", headers=auth_headers,
        )
        assert resp.status_code == 200
        _, args, _ = exec_.submitted[0]
        assert args[2] == 30

    def test_invalid_lookback_rejected(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        # Below ge=1
        resp = client.post(
            "/v1/admin/etl/run-eod?lookback_days=0", headers=auth_headers,
        )
        assert resp.status_code == 422


# ---------- POST: run-intraday ---------- #


class TestRunIntraday:

    def test_submits_runner(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/etl/run-intraday", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert exec_.submitted[0][0] == "_run_intraday_safe"


# ---------- POST: recompute-tiers ---------- #


class TestRecomputeTiers:

    def test_submits_runner(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/etl/recompute-tiers", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert exec_.submitted[0][0] == "_recompute_tiers_safe"


# ---------- POST: unpark-all (synchronous) ---------- #


class TestUnparkAll:

    def test_unpark_all_is_synchronous(self, auth_headers, monkeypatch):
        """unpark-all runs synchronously (no executor needed). We mock
        the runner.unpark_all directly to assert it was called and the
        count flows through."""
        # Override the actual unpark_all to return a known count
        import trip_ema.transports.http.routes.admin_etl as mod
        called = []
        def fake_unpark(engine, config):
            called.append((engine, config))
            return 7
        monkeypatch.setattr(mod, "unpark_all", fake_unpark)

        client = _make_client(executor=None)  # No executor needed
        resp = client.post("/v1/admin/etl/unpark-all", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "unpark-all"
        assert "Unparked 7 tickers" in body["notes"]
        assert len(called) == 1


# ---------- POST: backfill ---------- #


class TestBackfill:

    def test_with_all_tiers(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/etl/backfill?start_date=2020-01-01",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        _, args, _ = exec_.submitted[0]
        # args = (db_engine, config, start_date, tier_filter)
        assert args[2] == date(2020, 1, 1)
        assert args[3] is None

    def test_with_specific_tier(self, auth_headers):
        exec_ = FakeExecutor()
        client = _make_client(executor=exec_)
        resp = client.post(
            "/v1/admin/etl/backfill?start_date=2020-01-01&tier=1",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        _, args, _ = exec_.submitted[0]
        tier_filter = args[3]
        from trip_ema.etl.types import Tier
        assert tier_filter == {Tier.TIER_1}

    def test_invalid_tier_rejected(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/etl/backfill?start_date=2020-01-01&tier=5",
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_missing_start_date_rejected(self, auth_headers):
        client = _make_client(executor=FakeExecutor())
        resp = client.post(
            "/v1/admin/etl/backfill", headers=auth_headers,
        )
        assert resp.status_code == 422


# ---------- GET: status ---------- #


class TestStatusEndpoint:

    def test_composes_response_from_repo_and_tier_counts(
        self, auth_headers, monkeypatch,
    ):
        """Status is a read aggregator. Mock both the repo and the
        tier-counts helper and assert the JSON shape."""
        import trip_ema.transports.http.routes.admin_etl as mod

        # Stub the EtlStatusRepository's methods we use
        from trip_ema.etl.status_repo import RunRecord
        fake_recent = [
            RunRecord(
                run_id=1, kind="eod",
                started_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 5, 17, 10, 5, tzinfo=timezone.utc),
                outcome="success",
                tickers_attempted=2300, tickers_succeeded=2295,
                tickers_failed=5, bars_written=2295,
                correlation_id="corr-abc", notes="OK",
            ),
        ]

        class FakeRepo:
            def __init__(self, conn): pass
            def list_recent_runs(self, limit): return fake_recent
            def count_parked(self): return 12

        monkeypatch.setattr(mod, "EtlStatusRepository", FakeRepo)
        monkeypatch.setattr(
            mod, "_tier_counts",
            lambda conn, ex: mod.EtlTierCounts(tier_1=200, tier_2=500, tier_3=1600),
        )

        # Need a real-ish engine that supports `with engine.connect()`
        engine = MagicMock()
        # MagicMock's context manager returns another MagicMock — fine

        client = _make_client(db_engine=engine, executor=FakeExecutor())
        resp = client.get(
            "/v1/admin/etl/status?limit=5", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["recent_runs"]) == 1
        assert body["recent_runs"][0]["run_id"] == 1
        assert body["recent_runs"][0]["outcome"] == "success"
        assert body["parked_count"] == 12
        assert body["tier_counts"]["tier_1"] == 200
        assert body["tier_counts"]["tier_2"] == 500
        assert body["tier_counts"]["tier_3"] == 1600


# ---------- GET: parked ---------- #


class TestParkedEndpoint:

    def test_returns_parked_list_with_symbols(self, auth_headers):
        """The endpoint joins etl_ticker_status with tickers. We
        simulate the joined rows directly via the mocked Connection."""
        engine = MagicMock()
        conn = MagicMock()
        # `with engine.connect() as conn:` — set the context manager
        engine.connect.return_value.__enter__.return_value = conn

        # Fake the SQL result row objects
        row1 = MagicMock(
            ticker_id=42, symbol="RELIANCE",
            parked_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
            parked_reason="3 consecutive failures",
            consecutive_failures=3,
            last_error="yfinance timeout",
        )
        row2 = MagicMock(
            ticker_id=99, symbol="ZEEMEDIA",
            parked_at=None,
            parked_reason="auto-park: ticker delisted",
            consecutive_failures=5,
            last_error="symbol not found",
        )
        conn.execute.return_value = iter([row1, row2])

        client = _make_client(db_engine=engine, executor=FakeExecutor())
        resp = client.get("/v1/admin/etl/parked", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["parked"][0]["symbol"] == "RELIANCE"
        assert body["parked"][0]["consecutive_failures"] == 3
        assert body["parked"][1]["symbol"] == "ZEEMEDIA"
        assert body["parked"][1]["parked_at"] is None
