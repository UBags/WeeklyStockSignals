"""Tests for POST /v1/admin/reprime-ticker — postgres-free.

Uses dependency overrides to substitute the registry, cache, and
db_engine with in-process fakes. The route logic itself doesn't touch
SQL; it delegates to registry.reprime, which we stub.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.cache import SignalCache
from trip_ema.cache.signal_cache import build_cached_signal
from trip_ema.domain import (
    BarState,
    ChangeSignal,
    CoreRegime,
    IndicatorParams,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from trip_ema.engine import TickerEngine
from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    get_cache,
    get_compute_scheduler,
    get_config,
    get_db_engine,
    get_distributor,
    get_redis_queue,
    get_registry,
    require_admin_token,
)


# -------------------- Test doubles -------------------- #


class FakeRegistry:
    """Records calls to reprime; returns canned engines."""

    def __init__(self):
        self.engines: dict[tuple[TickerRef, Timeframe], TickerEngine] = {}
        self.reprime_calls: list[dict[str, Any]] = []
        # Configurable behavior knobs for tests
        self.next_reprime_raises: KeyError | None = None
        self.next_reprime_returns: bool = True

    def reprime(self, ref, timeframe, db_engine, *, refresh_metadata=False):
        self.reprime_calls.append({
            "ref": ref, "timeframe": timeframe,
            "refresh_metadata": refresh_metadata,
        })
        if self.next_reprime_raises is not None:
            raise self.next_reprime_raises
        return self.next_reprime_returns

    def get(self, ref, timeframe):
        return self.engines.get((ref, timeframe))

    # Methods the FastAPI app's lifespan checks don't need for these tests:
    def __len__(self):
        return len(self.engines)


class FakeDBEngine:
    @contextmanager
    def connect(self):
        yield MagicMock()
    @contextmanager
    def begin(self):
        yield MagicMock()


# -------------------- Helpers -------------------- #


def _build_engine_with_state(ref: TickerRef) -> TickerEngine:
    eng = TickerEngine(ref, Timeframe.WEEKLY, IndicatorParams())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = [100.0 + i * 0.5 for i in range(60)]
    bars = []
    for i, c in enumerate(closes):
        bars.append(OHLCVBar(
            timestamp=start + i * (start - start) if False else
                      datetime(2026, 1, 1, tzinfo=timezone.utc).replace(day=1) +
                      __import__("datetime").timedelta(days=7 * i),
            open=c, high=c + 1, low=c - 1, close=c, volume=1000.0,
        ))
    eng.prime(bars)
    return eng


# -------------------- Fixtures -------------------- #


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setenv("TRIP_EMA_ADMIN_TOKEN", "test-token-xyz")
    return "test-token-xyz"


@pytest.fixture
def fake_registry():
    return FakeRegistry()


@pytest.fixture
def cache():
    return SignalCache()


@pytest.fixture
def client(admin_token, fake_registry, cache):
    app = create_app()
    # Build a minimal Config stand-in: the admin gate only reads
    # config.http.admin_token_env to figure out which env var to consult.
    fake_config = MagicMock()
    fake_config.http.admin_token_env = "TRIP_EMA_ADMIN_TOKEN"
    app.dependency_overrides[get_config] = lambda: fake_config
    app.dependency_overrides[get_registry] = lambda: fake_registry
    app.dependency_overrides[get_cache] = lambda: cache
    app.dependency_overrides[get_db_engine] = lambda: FakeDBEngine()
    app.dependency_overrides[get_compute_scheduler] = lambda: MagicMock()
    app.dependency_overrides[get_distributor] = lambda: None
    app.dependency_overrides[get_redis_queue] = lambda: None
    return TestClient(app)


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# -------------------- Tests -------------------- #


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")


class TestRepriumeTicker:

    def test_successful_reprime_returns_200_and_refreshes_cache(
        self, client, fake_registry, cache, auth_headers,
    ):
        """Happy path: registry.reprime returns True, engine has state,
        cache gets a fresh entry, refresh_metadata=True is passed."""
        # Set up the engine that the registry will "return" post-reprime
        engine = _build_engine_with_state(NIFTY)
        fake_registry.engines[(NIFTY, Timeframe.WEEKLY)] = engine
        fake_registry.next_reprime_returns = True

        resp = client.post(
            "/v1/admin/reprime-ticker/NSE/NIFTY?instrument=INDEX",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["succeeded"] is True
        assert body["refreshed_metadata"] is True
        assert body["bars_loaded"] > 0
        assert body["ticker"]["symbol"] == "NIFTY"
        assert body["timeframe"] == "1W"

        # Registry was called with refresh_metadata=True
        assert len(fake_registry.reprime_calls) == 1
        call = fake_registry.reprime_calls[0]
        assert call["ref"] == NIFTY
        assert call["timeframe"] == Timeframe.WEEKLY
        assert call["refresh_metadata"] is True

        # Cache now has an entry
        cached = cache.get(NIFTY, Timeframe.WEEKLY)
        assert cached is not None
        assert cached.stale_since is None  # fresh prime clears stale flag

    def test_reprime_clears_pre_existing_stale_flag(
        self, client, fake_registry, cache, auth_headers,
    ):
        """If the ticker was stale before reprime, the new cache entry
        must NOT carry forward stale_since."""
        # Seed the cache with a stale entry
        engine = _build_engine_with_state(NIFTY)
        fake_registry.engines[(NIFTY, Timeframe.WEEKLY)] = engine
        old_state = engine.latest_state()
        stale_signal = build_cached_signal(
            ticker=NIFTY, timeframe=Timeframe.WEEKLY,
            latest_state=old_state, latest_close=100.0,
            state_history=engine.state_history(),
            last_change=ChangeSignal.NO_CHANGE,
            last_change_bar=None, last_change_computed_at=None,
            computed_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            stale_since=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        cache.put(stale_signal)
        assert cache.get(NIFTY, Timeframe.WEEKLY).stale_since is not None

        fake_registry.next_reprime_returns = True
        resp = client.post(
            "/v1/admin/reprime-ticker/NSE/NIFTY?instrument=INDEX",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Cache entry now has stale_since=None
        cached = cache.get(NIFTY, Timeframe.WEEKLY)
        assert cached.stale_since is None

    def test_reprime_unknown_ticker_returns_404(
        self, client, fake_registry, auth_headers,
    ):
        fake_registry.next_reprime_raises = KeyError("Unknown ticker: NSE:UNKNOWN")
        resp = client.post(
            "/v1/admin/reprime-ticker/NSE/UNKNOWN?instrument=EQ",
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "Unknown ticker" in resp.json()["detail"]

    def test_reprime_returning_false_means_no_bars(
        self, client, fake_registry, cache, auth_headers,
    ):
        """When registry.reprime returns False (ticker exists, no bars),
        the response is 200 with succeeded=False and a clear note."""
        fake_registry.next_reprime_returns = False
        # No engine in registry.engines after a False return
        resp = client.post(
            "/v1/admin/reprime-ticker/NSE/NIFTY?instrument=INDEX",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["succeeded"] is False
        assert body["bars_loaded"] == 0
        assert body["refreshed_metadata"] is True
        assert "no bars" in body["notes"].lower()

    def test_reprime_requires_admin_token(self, client, fake_registry):
        """Without the bearer header, the endpoint must return 401."""
        resp = client.post(
            "/v1/admin/reprime-ticker/NSE/NIFTY?instrument=INDEX",
        )
        assert resp.status_code == 401

    def test_reprime_with_wrong_token_returns_403(self, client, fake_registry):
        resp = client.post(
            "/v1/admin/reprime-ticker/NSE/NIFTY?instrument=INDEX",
            headers={"Authorization": "Bearer not-the-real-token"},
        )
        assert resp.status_code == 403

    def test_reprime_default_instrument_and_timeframe(
        self, client, fake_registry, auth_headers,
    ):
        """Without explicit query params, instrument defaults to 'EQ'
        and timeframe to '1W'."""
        fake_registry.next_reprime_returns = True
        # Need to seed an engine matching the default-built ref
        default_ref = TickerRef("NSE", "TCS", "EQ")
        engine = _build_engine_with_state(default_ref)
        fake_registry.engines[(default_ref, Timeframe.WEEKLY)] = engine

        resp = client.post(
            "/v1/admin/reprime-ticker/NSE/TCS",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        call = fake_registry.reprime_calls[0]
        assert call["ref"].instrument == "EQ"
        assert call["timeframe"] == Timeframe.WEEKLY
