"""Tests for GET /v1/admin/circuit-breakers.

Postgres-free; uses dependency overrides and a real
CircuitBreakingTransport wrapping a FakeTransport so we can verify
state transitions show up correctly in the response.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.notifications.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakingTransport,
    CircuitState,
)
from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    get_cache,
    get_compute_scheduler,
    get_config,
    get_db_engine,
    get_distributor,
    get_redis_queue,
    get_registry,
)


# Reuse the FakeTransport from the circuit-breaker tests
from tests.notifications.test_circuit_breaker import FakeTransport, _make_batch


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setenv("TRIP_EMA_ADMIN_TOKEN", "test-token-xyz")
    return "test-token-xyz"


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _make_config(breakers_enabled=True, redis_set=False):
    cfg = MagicMock()
    cfg.http.admin_token_env = "TRIP_EMA_ADMIN_TOKEN"
    cfg.transports.circuit_breaker.enabled = breakers_enabled
    cfg.redis = MagicMock() if redis_set else None
    return cfg


def _make_client(config, distributor):
    app = create_app()
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_distributor] = lambda: distributor
    app.dependency_overrides[get_registry] = lambda: MagicMock()
    app.dependency_overrides[get_cache] = lambda: MagicMock()
    app.dependency_overrides[get_db_engine] = lambda: MagicMock()
    app.dependency_overrides[get_compute_scheduler] = lambda: MagicMock()
    app.dependency_overrides[get_redis_queue] = lambda: None
    return TestClient(app)


class TestCircuitBreakersEndpoint:

    def test_disabled_mode_returns_empty_list(self, auth_headers):
        config = _make_config(breakers_enabled=False)
        client = _make_client(config, distributor=None)
        resp = client.get("/v1/admin/circuit-breakers", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "disabled"
        assert body["breakers"] == []
        assert "disabled" in body["notes"].lower()

    def test_queue_mode_explains_state_lives_elsewhere(self, auth_headers):
        """When Redis is configured, transports live in the notifier
        subprocess — the API can't introspect them. Endpoint must say
        so honestly rather than report fabricated state."""
        config = _make_config(breakers_enabled=True, redis_set=True)
        client = _make_client(config, distributor=None)
        resp = client.get("/v1/admin/circuit-breakers", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "queue"
        assert body["breakers"] == []
        assert "notifier worker process" in body["notes"]

    def test_legacy_mode_with_no_distributor(self, auth_headers):
        config = _make_config(breakers_enabled=True, redis_set=False)
        client = _make_client(config, distributor=None)
        resp = client.get("/v1/admin/circuit-breakers", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "legacy"
        assert body["breakers"] == []

    def test_legacy_mode_reports_breaker_in_closed_state(self, auth_headers):
        config = _make_config(breakers_enabled=True, redis_set=False)
        # Build a real CircuitBreakingTransport so the endpoint reads
        # real snapshot() output.
        fake = FakeTransport()
        cb = CircuitBreakingTransport(
            fake, name="email",
            config=CircuitBreakerConfig(failure_threshold=3),
        )
        distributor = MagicMock()
        distributor.transports = {"email": cb}

        client = _make_client(config, distributor=distributor)
        resp = client.get("/v1/admin/circuit-breakers", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "legacy"
        assert len(body["breakers"]) == 1
        entry = body["breakers"][0]
        assert entry["name"] == "email"
        assert entry["state"] == "closed"
        assert entry["consecutive_failures"] == 0
        assert entry["opened_at_monotonic"] is None
        assert entry["config"]["failure_threshold"] == 3

    def test_legacy_mode_reports_breaker_in_open_state(self, auth_headers):
        """After tripping the breaker, the endpoint must report
        state=open and a non-None opened_at_monotonic."""
        config = _make_config(breakers_enabled=True, redis_set=False)
        fake = FakeTransport()
        fake.queue(False, False)
        cb = CircuitBreakingTransport(
            fake, name="webhook",
            config=CircuitBreakerConfig(failure_threshold=2),
        )
        cb.send("a", _make_batch())
        cb.send("a", _make_batch())
        assert cb.state == CircuitState.OPEN

        distributor = MagicMock()
        distributor.transports = {"webhook": cb}

        client = _make_client(config, distributor=distributor)
        resp = client.get("/v1/admin/circuit-breakers", headers=auth_headers)
        assert resp.status_code == 200
        entry = resp.json()["breakers"][0]
        assert entry["state"] == "open"
        assert entry["consecutive_failures"] == 2
        assert entry["opened_at_monotonic"] is not None
        # Sanity: it's a recent monotonic value (not in the far past)
        assert entry["opened_at_monotonic"] > 0

    def test_legacy_mode_with_multiple_transports(self, auth_headers):
        """Two breakers — one CLOSED, one OPEN — both reported with
        their own state. Verifies per-transport isolation surfaces
        correctly through the endpoint."""
        config = _make_config(breakers_enabled=True, redis_set=False)
        fake_email = FakeTransport()
        cb_email = CircuitBreakingTransport(
            fake_email, name="email",
            config=CircuitBreakerConfig(failure_threshold=3),
        )

        fake_webhook = FakeTransport()
        fake_webhook.queue(False, False)
        cb_webhook = CircuitBreakingTransport(
            fake_webhook, name="webhook",
            config=CircuitBreakerConfig(failure_threshold=2),
        )
        cb_webhook.send("a", _make_batch())
        cb_webhook.send("a", _make_batch())

        distributor = MagicMock()
        distributor.transports = {
            "email": cb_email,
            "webhook": cb_webhook,
        }

        client = _make_client(config, distributor=distributor)
        resp = client.get("/v1/admin/circuit-breakers", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["breakers"]) == 2
        by_name = {b["name"]: b for b in body["breakers"]}
        assert by_name["email"]["state"] == "closed"
        assert by_name["webhook"]["state"] == "open"

    def test_legacy_mode_skips_unwrapped_transport(self, auth_headers):
        """A transport that's NOT a CircuitBreakingTransport (shouldn't
        happen in production, but defensive) must be skipped rather
        than crashing the endpoint."""
        config = _make_config(breakers_enabled=True, redis_set=False)
        fake = FakeTransport()  # not wrapped
        distributor = MagicMock()
        distributor.transports = {"email": fake}

        client = _make_client(config, distributor=distributor)
        resp = client.get("/v1/admin/circuit-breakers", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["breakers"] == []

    def test_requires_admin_token(self):
        config = _make_config(breakers_enabled=True, redis_set=False)
        # Don't use admin_token fixture — env var unset → 503
        # Actually need to ensure env var is NOT set
        import os
        os.environ.pop("TRIP_EMA_ADMIN_TOKEN", None)
        client = _make_client(config, distributor=None)
        resp = client.get("/v1/admin/circuit-breakers")
        # Either 503 (env unset) or 401 (env set but no header)
        assert resp.status_code in (401, 503)
