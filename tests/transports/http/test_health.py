"""Tests for /healthz and /readyz."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from trip_ema.cache import SignalCache
from trip_ema.cache.signal_cache import (
    CachedSnapshot,
    IndicatorStateSnapshot,
)
from trip_ema.domain import TickerRef, Timeframe
from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    get_cache,
    get_compute_scheduler,
    get_config,
    get_distributor,
    get_redis_queue,
)
from tests.transports.http.conftest import _build_test_config


def _populate_cache(cache: SignalCache) -> None:
    """Seed the cache with one snapshot so readyz returns 200."""
    t = datetime(2026, 5, 18, tzinfo=timezone.utc)
    ind = IndicatorStateSnapshot(
        trade_date=date(2026, 5, 18),
        med_ema=100.0, long_ema=100.0, longest_ema=100.0, atr=2.0,
        upper_inner_band=102.0, lower_inner_band=98.0,
        upper_mid_band=105.0,   lower_mid_band=95.0,
        upper_outer_band=110.0, lower_outer_band=90.0,
        display_regime=1,
        computed_at=t,
    )
    ref = TickerRef("NSE", "NIFTY", "INDEX")
    cache.put_snapshot(CachedSnapshot(
        ticker_id=1,
        ticker_ref=ref,
        timeframe=Timeframe.WEEKLY,
        latest_indicator_state=ind,
        latest_signal_event=None,
        snapshot_taken_at=t,
    ))


def test_healthz_returns_200_on_empty_state(http_app):
    """healthz is liveness — should pass even with nothing primed."""
    response = http_app.client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # registry_size is a legacy wire field that mirrors cache_size now.
    assert body["registry_size"] == 0
    assert body["cache_size"] == 0


def test_readyz_returns_503_on_empty_state(http_app):
    """readyz requires a populated cache."""
    response = http_app.client.get("/readyz")
    assert response.status_code == 503


def test_readyz_returns_200_when_primed(http_app):
    _populate_cache(http_app.cache)
    response = http_app.client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["cache_size"] == 1
    assert body["registry_size"] == 1   # mirrors cache_size


def test_healthz_reports_sizes(http_app):
    _populate_cache(http_app.cache)
    response = http_app.client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["cache_size"] == 1
    assert body["registry_size"] == 1


# ============================================================
# Phase 7: thread liveness in /readyz
# ============================================================


class _StubScheduler:
    """Configurable health-check stub."""
    def __init__(self, *, alive: bool, last_run, unhealthy_reason: str | None):
        self._alive = alive
        self.last_run_completed_at = last_run
        self._reason = unhealthy_reason

    def is_alive(self):
        return self._alive

    def is_unhealthy(self, now, stale_after_multiplier=2.0):
        return self._reason


def _build_app_with(scheduler, distributor=None, redis_queue=None):
    """Build a fresh TestClient with the stubs we want, cache pre-seeded
    so the only failure axes are the scheduler / distributor / redis
    stubs."""
    cache = SignalCache()
    _populate_cache(cache)

    app = create_app()
    app.dependency_overrides[get_cache] = lambda: cache
    app.dependency_overrides[get_config] = lambda: _build_test_config()
    app.dependency_overrides[get_compute_scheduler] = lambda: scheduler
    app.dependency_overrides[get_distributor] = lambda: distributor
    app.dependency_overrides[get_redis_queue] = lambda: redis_queue
    return TestClient(app), app


def test_readyz_503_when_compute_scheduler_died():
    """If the scheduler reports unhealthy (e.g., thread died), /readyz
    must return 503 even if cache is populated."""
    scheduler = _StubScheduler(
        alive=False,
        last_run=datetime(2026, 5, 18, tzinfo=timezone.utc),
        unhealthy_reason="ComputeScheduler thread died",
    )
    client, app = _build_app_with(scheduler)
    try:
        r = client.get("/readyz")
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert any("ComputeScheduler" in s for s in detail["issues"])
    finally:
        app.dependency_overrides.clear()


def test_readyz_503_when_distributor_died():
    scheduler = _StubScheduler(
        alive=True,
        last_run=datetime.now(timezone.utc),
        unhealthy_reason=None,
    )
    distributor = _StubScheduler(
        alive=False,
        last_run=datetime(2026, 5, 18, tzinfo=timezone.utc),
        unhealthy_reason="SubscriberDistributor thread died",
    )
    client, app = _build_app_with(scheduler, distributor=distributor)
    try:
        r = client.get("/readyz")
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert any("SubscriberDistributor" in s for s in detail["issues"])
    finally:
        app.dependency_overrides.clear()


def test_readyz_503_when_scheduler_wedged():
    """Thread alive but no completion in 2× cadence → unhealthy."""
    scheduler = _StubScheduler(
        alive=True,
        last_run=datetime(2026, 5, 18, tzinfo=timezone.utc),
        unhealthy_reason="ComputeScheduler hasn't completed in 1800s",
    )
    client, app = _build_app_with(scheduler)
    try:
        r = client.get("/readyz")
        assert r.status_code == 503
        assert any("completed in" in s for s in r.json()["detail"]["issues"])
    finally:
        app.dependency_overrides.clear()


def test_readyz_503_when_redis_unreachable():
    """Redis configured but unreachable → unhealthy."""
    scheduler = _StubScheduler(
        alive=True, last_run=None, unhealthy_reason=None,
    )
    class FakeQueue:
        def ping(self):
            return False
    client, app = _build_app_with(scheduler, redis_queue=FakeQueue())
    try:
        r = client.get("/readyz")
        assert r.status_code == 503
        assert any("Redis" in s for s in r.json()["detail"]["issues"])
    finally:
        app.dependency_overrides.clear()


def test_readyz_200_when_redis_reachable():
    """Redis configured and reachable → no issue."""
    scheduler = _StubScheduler(
        alive=True, last_run=None, unhealthy_reason=None,
    )
    class FakeQueue:
        def ping(self):
            return True
    client, app = _build_app_with(scheduler, redis_queue=FakeQueue())
    try:
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["redis_reachable"] is True
    finally:
        app.dependency_overrides.clear()


def test_readyz_lists_all_issues():
    """Multiple problems should all surface in the response."""
    scheduler = _StubScheduler(
        alive=False, last_run=None,
        unhealthy_reason="ComputeScheduler thread died",
    )
    class FakeQueue:
        def ping(self):
            return False
    client, app = _build_app_with(scheduler, redis_queue=FakeQueue())
    try:
        r = client.get("/readyz")
        assert r.status_code == 503
        issues = r.json()["detail"]["issues"]
        assert len(issues) >= 2
    finally:
        app.dependency_overrides.clear()