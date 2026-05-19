"""Tests for POST /v1/admin/* endpoints.

These require both auth (bearer token) and DB connectivity (prime-all
actually queries tickers from the DB). We use the real db_engine fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from trip_ema.cache import SignalCache
from trip_ema.domain import (
    IndicatorParams,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from trip_ema.persistence import (
    BarsRepository,
    TickersRepository,
    transaction,
)
from trip_ema.registry import TickerRegistry
from trip_ema.scheduler import ComputeScheduler
from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    get_cache,
    get_compute_scheduler,
    get_config,
    get_db_engine,
    get_registry,
)

from tests.transports.http.conftest import _build_test_config


def _bar_seed_start(n: int) -> datetime:
    """Compute a seed-start timestamp such that the last bar in a
    weekly series of length `n` lands one week before today (UTC).

    Tests originally hard-coded `datetime(2024, 1, 1, ...)`, but the
    ComputeScheduler's gap detector compares high-water bar timestamps
    against today — once the gap grows past a few weeks, runs route
    through the gap-recovery path and `tickers_with_new_bars` silently
    becomes 0. Anchoring to today removes that time-bound coupling.
    Mirrors the helper of the same name in tests/scheduler/
    test_compute_scheduler.py."""
    today_utc = datetime.now(timezone.utc).date()
    last_bar = datetime.combine(
        today_utc, datetime.min.time(), tzinfo=timezone.utc,
    ) - timedelta(weeks=1)
    return last_bar - timedelta(weeks=(n - 1))


@pytest.fixture
def admin_token(monkeypatch):
    """Set the admin token env var to a known value, then unset on teardown."""
    monkeypatch.setenv("TRIP_EMA_ADMIN_TOKEN", "test-secret-abc123")
    return "test-secret-abc123"


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def admin_client(db_engine):
    """A TestClient wired to a real DB plus empty cache/registry/scheduler.
    Used by tests that exercise the admin endpoints' real behavior."""
    cache = SignalCache()
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    scheduler = ComputeScheduler(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    config = _build_test_config()

    app = create_app()
    app.dependency_overrides[get_db_engine] = lambda: db_engine
    app.dependency_overrides[get_cache] = lambda: cache
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_compute_scheduler] = lambda: scheduler
    app.dependency_overrides[get_config] = lambda: config
    with TestClient(app) as client:
        yield client, db_engine, cache, registry, scheduler
    app.dependency_overrides.clear()


# ============================================================
# Auth
# ============================================================


def test_admin_endpoint_requires_token(admin_client, admin_token):
    client, _, _, _, _ = admin_client
    response = client.post("/v1/admin/prime-all")
    assert response.status_code == 401


def test_admin_endpoint_rejects_wrong_token(admin_client, admin_token):
    client, _, _, _, _ = admin_client
    response = client.post(
        "/v1/admin/prime-all",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 403


def test_admin_endpoint_rejects_malformed_header(admin_client, admin_token):
    client, _, _, _, _ = admin_client
    response = client.post(
        "/v1/admin/prime-all",
        headers={"Authorization": admin_token},   # missing 'Bearer '
    )
    assert response.status_code == 401


def test_admin_returns_503_when_token_env_unset(admin_client, monkeypatch):
    """When TRIP_EMA_ADMIN_TOKEN is not in the environment, admin endpoints
    are disabled (503)."""
    monkeypatch.delenv("TRIP_EMA_ADMIN_TOKEN", raising=False)
    client, _, _, _, _ = admin_client
    response = client.post(
        "/v1/admin/prime-all",
        headers={"Authorization": "Bearer anything"},
    )
    assert response.status_code == 503


# ============================================================
# prime-all
# ============================================================


def test_prime_all_with_empty_db(admin_client, auth_headers):
    """No tickers in DB → tickers_primed=0."""
    client, _, _, _, _ = admin_client
    response = client.post("/v1/admin/prime-all", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["tickers_primed"] == 0
    assert body["cache_entries_populated"] == 0


def test_prime_all_with_seeded_ticker(admin_client, auth_headers):
    """Seed a ticker with bars; prime-all should pull it into the registry
    and cache."""
    client, db_engine, cache, registry, _ = admin_client

    # Seed
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(
            TickerRef("NSE", "NIFTY", "INDEX"),
            created_at=datetime.now(timezone.utc),
        )
        bars_repo = BarsRepository(conn)
        start = _bar_seed_start(60)
        for i in range(60):
            bars_repo.insert_bar(
                tid, Timeframe.WEEKLY,
                OHLCVBar(
                    timestamp=start + timedelta(weeks=i),
                    open=100 + i, high=101 + i, low=99 + i,
                    close=100.5 + i, volume=1000.0,
                ),
            )

    response = client.post("/v1/admin/prime-all", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["tickers_primed"] == 1
    assert body["cache_entries_populated"] == 1
    assert len(cache) == 1
    assert len(registry) == 1


# ============================================================
# compute-now
# ============================================================


def test_compute_now_on_empty_registry(admin_client, auth_headers):
    """No tickers in registry → run_once examines 0 tickers."""
    client, _, _, _, _ = admin_client
    response = client.post("/v1/admin/compute-now", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["tickers_examined"] == 0
    assert body["events_fired"] == 0
    assert body["errors"] == []


def test_compute_now_returns_run_summary(admin_client, auth_headers):
    """After prime-all + new bar, compute-now should return a populated summary."""
    client, db_engine, cache, registry, _ = admin_client

    # Seed and prime
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(
            TickerRef("NSE", "NIFTY", "INDEX"),
            created_at=datetime.now(timezone.utc),
        )
        bars_repo = BarsRepository(conn)
        start = _bar_seed_start(60)
        for i in range(60):
            bars_repo.insert_bar(
                tid, Timeframe.WEEKLY,
                OHLCVBar(
                    timestamp=start + timedelta(weeks=i),
                    open=100 + i, high=101 + i, low=99 + i,
                    close=100.5 + i, volume=1000.0,
                ),
            )

    prime_resp = client.post("/v1/admin/prime-all", headers=auth_headers)
    assert prime_resp.status_code == 200

    # Add a new bar
    with transaction(db_engine) as conn:
        BarsRepository(conn).insert_bar(
            tid, Timeframe.WEEKLY,
            OHLCVBar(
                timestamp=start + timedelta(weeks=60),
                open=160, high=161, low=159, close=160.5, volume=1000.0,
            ),
        )

    response = client.post("/v1/admin/compute-now", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["tickers_examined"] == 1
    assert body["tickers_with_new_bars"] == 1
    assert body["bars_ingested"] == 1
