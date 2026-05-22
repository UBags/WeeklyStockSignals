"""HTTP route tests for /v1/fundamentals/*.

Pattern matches the existing admin_etl route tests:
  - http_app fixture from conftest provides a TestClient.
  - db_engine fixture provides a real Postgres with Alembic head.
  - We override get_db_engine to point at our test engine.
  - We set TRIP_EMA_ADMIN_TOKEN in os.environ for the require_admin_token
    gate. tests/transports/http/conftest.py already sets the default.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.domain import TickerRef
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndustryOverrideRepository,
    InstrumentIndustryRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository
from trip_ema.transports.http.dependencies import (
    get_db_engine,
    get_etl_executor,
)


# ---------- fixtures ---------- #


AUTH_HEADERS = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def _admin_token_env(monkeypatch):
    monkeypatch.setenv("TRIP_EMA_ADMIN_TOKEN", "test-token")


@pytest.fixture
def reliance_ticker(db_engine):
    ref = TickerRef("NSE", "RELIANCE", "EQ")
    with transaction(db_engine) as conn:
        return TickersRepository(conn).insert(
            ref, created_at=datetime.now(timezone.utc),
        )


@pytest.fixture
def real_db_client(http_app, db_engine):
    """Wire the test client to the real DB fixture. Returns the client."""
    http_app.client.app.dependency_overrides[get_db_engine] = lambda: db_engine
    # Most routes also want an executor; provide a stub that runs
    # submitted tasks synchronously (so tests can see the result
    # without sleeping).
    class _SyncExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)
            return MagicMock()
    http_app.client.app.dependency_overrides[get_etl_executor] = lambda: _SyncExecutor()
    return http_app.client


# ---------- GET /industry ---------- #


class TestListIndustry:

    def test_returns_empty_list_for_unclassified_exchange(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/industry?exchange=NSE",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json() == {"exchange": "NSE", "rows": []}

    def test_returns_rows(self, real_db_client, db_engine, reliance_ticker):
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert(
                reliance_ticker, sector="Energy", industry="Oil & Gas",
            )
        r = real_db_client.get(
            "/v1/fundamentals/industry?exchange=NSE",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["exchange"] == "NSE"
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["ticker_id"] == reliance_ticker
        assert row["sector"] == "Energy"
        assert row["industry"] == "Oil & Gas"
        assert row["classification_source"] == "YFINANCE"

    def test_rejects_invalid_exchange(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/industry?exchange=BOGUS",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400


# ---------- GET /industry/{ticker_id} ---------- #


class TestGetIndustry:

    def test_returns_404_for_unknown(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/industry/999999",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 404

    def test_returns_row(self, real_db_client, db_engine, reliance_ticker):
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert(
                reliance_ticker, sector="Energy", industry="Oil",
            )
        r = real_db_client.get(
            f"/v1/fundamentals/industry/{reliance_ticker}",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["industry"] == "Oil"


# ---------- GET /coverage ---------- #


class TestCoverage:

    def test_coverage_empty(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/coverage",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json() == {"rows": []}

    def test_coverage_with_data(self, real_db_client, db_engine, reliance_ticker):
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert(
                reliance_ticker, sector="Energy", industry="Oil",
            )
        r = real_db_client.get(
            "/v1/fundamentals/coverage",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        nse = next(row for row in body["rows"] if row["exchange"] == "NSE")
        assert nse["total"] == 1
        assert nse["classified"] == 1
        assert nse["pct"] == 100.0


# ---------- GET /runs ---------- #


class TestListRuns:

    def test_returns_empty_when_no_runs(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/runs",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json() == {"rows": []}

    def test_returns_runs_with_filter(self, real_db_client, db_engine):
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="industry", exchange="NSE")
            r.close(rid, status="SUCCESS", succeeded=10, failed=0, skipped=0)
        r = real_db_client.get(
            "/v1/fundamentals/runs?phase=industry&exchange=NSE",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["status"] == "SUCCESS"
        assert body["rows"][0]["succeeded"] == 10


# ---------- POST /refresh-industry ---------- #


class TestRefreshIndustry:

    def test_returns_202_and_kicks_runner(
        self, real_db_client, db_engine, reliance_ticker, monkeypatch,
    ):
        # Patch run_industry_refresh to verify it gets called.
        called = []
        def fake_runner(engine, exchange, *, force=False, **kwargs):
            called.append((exchange, force))
            return 0
        monkeypatch.setattr(
            "trip_ema.transports.http.routes.fundamentals.run_industry_refresh",
            fake_runner,
        )
        r = real_db_client.post(
            "/v1/fundamentals/refresh-industry?exchange=NSE&force=true",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 202
        body = r.json()
        assert body["action"] == "refresh-industry"
        # The sync executor in real_db_client ran the runner inline.
        assert called == [("NSE", True)]

    def test_rejects_invalid_exchange(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/refresh-industry?exchange=BOGUS",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400


# ---------- POST /override-industry ---------- #


class TestOverrideIndustry:

    def test_sets_override(self, real_db_client, db_engine, reliance_ticker):
        r = real_db_client.post(
            "/v1/fundamentals/override-industry",
            json={
                "ticker_id": reliance_ticker,
                "sector": "Energy",
                "industry": "Hand-set",
                "note": "ops decision",
                "set_by": "uddi",
            },
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json() == {"ticker_id": reliance_ticker, "status": "set"}
        with db_engine.connect() as conn:
            ov = IndustryOverrideRepository(conn).get(reliance_ticker)
        assert ov.industry == "Hand-set"

    def test_returns_404_for_unknown_ticker(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/override-industry",
            json={"ticker_id": 999_999, "sector": "X", "industry": "Y"},
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 404

    def test_rejects_empty_industry(self, real_db_client, reliance_ticker):
        r = real_db_client.post(
            "/v1/fundamentals/override-industry",
            json={"ticker_id": reliance_ticker, "sector": "X", "industry": ""},
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 422


# ---------- DELETE /override-industry/{tid} ---------- #


class TestDeleteOverride:

    def test_removes_override(self, real_db_client, db_engine, reliance_ticker):
        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                reliance_ticker, sector="X", industry="Y",
            )
        r = real_db_client.delete(
            f"/v1/fundamentals/override-industry/{reliance_ticker}",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "removed"

    def test_returns_404_when_no_override(self, real_db_client, reliance_ticker):
        r = real_db_client.delete(
            f"/v1/fundamentals/override-industry/{reliance_ticker}",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 404


# ---------- auth ---------- #


def test_endpoints_require_auth(real_db_client):
    """Missing Authorization header → 401."""
    r = real_db_client.get("/v1/fundamentals/coverage")
    assert r.status_code == 401


def test_endpoints_reject_bad_token(real_db_client):
    r = real_db_client.get(
        "/v1/fundamentals/coverage",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 403


# ============================================================
# Phase 2 — shares endpoints
# ============================================================


from datetime import date
from trip_ema.persistence.fundamentals_repo import InstrumentSharesRepository


class TestListShares:

    def test_returns_empty_for_exchange_with_no_shares(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/shares?exchange=NSE",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json() == {"exchange": "NSE", "rows": []}

    def test_returns_latest_per_ticker(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        # Two snapshots on different dates — endpoint returns latest.
        with transaction(db_engine) as conn:
            r = InstrumentSharesRepository(conn)
            r.insert_snapshot(
                reliance_ticker, as_of_date=date(2025, 12, 1),
                shares_outstanding=6_000_000_000,
                shares_source="YFINANCE",
            )
            r.insert_snapshot(
                reliance_ticker, as_of_date=date(2026, 5, 22),
                shares_outstanding=6_768_000_000,
                shares_source="YFINANCE",
            )
        r = real_db_client.get(
            "/v1/fundamentals/shares?exchange=NSE",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert r.status_code == 200
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["ticker_id"] == reliance_ticker
        assert row["shares_outstanding"] == 6_768_000_000
        assert row["as_of_date"] == "2026-05-22"
        assert row["shares_source"] == "YFINANCE"

    def test_rejects_invalid_exchange(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/shares?exchange=BOGUS",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400


class TestGetLatestShares:

    def test_returns_404_for_unknown(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/shares/999999",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 404

    def test_returns_row(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        with transaction(db_engine) as conn:
            InstrumentSharesRepository(conn).insert_snapshot(
                reliance_ticker, as_of_date=date(2026, 5, 22),
                shares_outstanding=6_768_000_000,
                shares_source="YFINANCE",
            )
        r = real_db_client.get(
            f"/v1/fundamentals/shares/{reliance_ticker}",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["shares_outstanding"] == 6_768_000_000


class TestGetSharesHistory:

    def test_returns_history_newest_first(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        with transaction(db_engine) as conn:
            r = InstrumentSharesRepository(conn)
            r.insert_snapshot(
                reliance_ticker, as_of_date=date(2025, 12, 1),
                shares_outstanding=6_000_000_000, shares_source="YFINANCE",
            )
            r.insert_snapshot(
                reliance_ticker, as_of_date=date(2026, 3, 1),
                shares_outstanding=6_500_000_000, shares_source="YFINANCE",
            )
            r.insert_snapshot(
                reliance_ticker, as_of_date=date(2026, 5, 22),
                shares_outstanding=6_768_000_000, shares_source="YFINANCE",
            )
        r = real_db_client.get(
            f"/v1/fundamentals/shares/{reliance_ticker}/history",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        rows = body["rows"]
        assert len(rows) == 3
        assert rows[0]["as_of_date"] == "2026-05-22"
        assert rows[1]["as_of_date"] == "2026-03-01"
        assert rows[2]["as_of_date"] == "2025-12-01"

    def test_history_respects_limit(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        with transaction(db_engine) as conn:
            r = InstrumentSharesRepository(conn)
            for m in (1, 2, 3, 4, 5):
                r.insert_snapshot(
                    reliance_ticker, as_of_date=date(2026, m, 1),
                    shares_outstanding=m * 1_000,
                    shares_source="YFINANCE",
                )
        r = real_db_client.get(
            f"/v1/fundamentals/shares/{reliance_ticker}/history?limit=2",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert len(body["rows"]) == 2

    def test_returns_empty_history_for_unknown(self, real_db_client):
        """No 404 for empty history — returns an empty rows list."""
        r = real_db_client.get(
            "/v1/fundamentals/shares/999999/history",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["rows"] == []


class TestRefreshShares:

    def test_returns_202_and_kicks_runner(
        self, real_db_client, db_engine, reliance_ticker, monkeypatch,
    ):
        called = []
        def fake_runner(engine, exchange, *, force=False, **kw):
            called.append((exchange, force))
            return 0
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_shares_refresh",
            fake_runner,
        )
        r = real_db_client.post(
            "/v1/fundamentals/refresh-shares?exchange=NSE&force=true",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 202
        body = r.json()
        assert body["action"] == "refresh-shares"
        assert called == [("NSE", True)]

    def test_rejects_invalid_exchange(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/refresh-shares?exchange=BOGUS",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400


def test_shares_endpoints_require_auth(real_db_client):
    """No Authorization → 401, matching the rest of /v1/fundamentals/*."""
    r = real_db_client.get("/v1/fundamentals/shares?exchange=NSE")
    assert r.status_code == 401
