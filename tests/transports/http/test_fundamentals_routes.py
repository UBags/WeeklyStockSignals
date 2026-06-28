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
from datetime import datetime, timezone, UTC
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
            ref, created_at=datetime.now(UTC),
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


from datetime import date, UTC
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


# ============================================================
# Phase 3 — quarterly financials endpoints
# ============================================================


from trip_ema.persistence.fundamentals_repo import QuarterlyFinancialsRepository


class TestListFinancials:

    def test_returns_empty_for_exchange_with_no_data(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/financials?exchange=NSE",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json() == {"exchange": "NSE", "rows": []}

    def test_returns_latest_quarter_per_ticker(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        with transaction(db_engine) as conn:
            r = QuarterlyFinancialsRepository(conn)
            r.insert_snapshot(
                reliance_ticker, fiscal_quarter=date(2025, 9, 30),
                revenue=2400e9, ebitda=340e9,
            )
            r.insert_snapshot(
                reliance_ticker, fiscal_quarter=date(2025, 12, 31),
                revenue=2500e9, ebitda=350e9,
            )
        r = real_db_client.get(
            "/v1/fundamentals/financials?exchange=NSE",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert r.status_code == 200
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["ticker_id"] == reliance_ticker
        # Latest quarter wins.
        assert row["fiscal_quarter"] == "2025-12-31"
        assert row["revenue"] == 2500e9

    def test_rejects_invalid_exchange(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/financials?exchange=BOGUS",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400


class TestGetLatestNQuarters:

    def test_returns_n_quarters_newest_first(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        with transaction(db_engine) as conn:
            r = QuarterlyFinancialsRepository(conn)
            for fq, rev in [
                (date(2025, 3, 31),   100.0),
                (date(2025, 6, 30),   110.0),
                (date(2025, 9, 30),   120.0),
                (date(2025, 12, 31),  130.0),
            ]:
                r.insert_snapshot(
                    reliance_ticker, fiscal_quarter=fq,
                    revenue=rev, ebitda=None,
                )
        r = real_db_client.get(
            f"/v1/fundamentals/financials/{reliance_ticker}/quarters?n=4",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert r.status_code == 200
        revs = [row["revenue"] for row in body["rows"]]
        assert revs == [130.0, 120.0, 110.0, 100.0]

    def test_n_default_is_4(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        with transaction(db_engine) as conn:
            r = QuarterlyFinancialsRepository(conn)
            for m in (1, 2, 3, 4, 5):
                r.insert_snapshot(
                    reliance_ticker, fiscal_quarter=date(2026, m, 1),
                    revenue=m * 1.0, ebitda=None,
                )
        r = real_db_client.get(
            f"/v1/fundamentals/financials/{reliance_ticker}/quarters",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert len(body["rows"]) == 4

    def test_rejects_n_above_20(self, real_db_client, reliance_ticker):
        r = real_db_client.get(
            f"/v1/fundamentals/financials/{reliance_ticker}/quarters?n=100",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 422


class TestGetQuarterHistory:

    def test_returns_history_newest_first(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        import time as _t
        with transaction(db_engine) as conn:
            QuarterlyFinancialsRepository(conn).insert_snapshot(
                reliance_ticker, fiscal_quarter=date(2025, 9, 30),
                revenue=100.0, ebitda=15.0,
            )
        _t.sleep(0.01)
        with transaction(db_engine) as conn:
            QuarterlyFinancialsRepository(conn).insert_snapshot(
                reliance_ticker, fiscal_quarter=date(2025, 9, 30),
                revenue=99.0, ebitda=14.5,  # restated
            )
        r = real_db_client.get(
            f"/v1/fundamentals/financials/{reliance_ticker}"
            f"/quarter/2025-09-30/history",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert r.status_code == 200
        assert len(body["rows"]) == 2
        # Newest first.
        assert body["rows"][0]["revenue"] == 99.0
        assert body["rows"][1]["revenue"] == 100.0


class TestRefreshFinancials:

    def test_returns_202_and_kicks_runner(
        self, real_db_client, db_engine, reliance_ticker, monkeypatch,
    ):
        called = []
        def fake_runner(engine, exchange, *, force=False, **kw):
            called.append((exchange, force))
            return 0
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_financials_refresh",
            fake_runner,
        )
        r = real_db_client.post(
            "/v1/fundamentals/refresh-financials?exchange=NSE&force=true",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 202
        body = r.json()
        assert body["action"] == "refresh-financials"
        assert called == [("NSE", True)]

    def test_rejects_invalid_exchange(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/refresh-financials?exchange=BOGUS",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400


def test_financials_endpoints_require_auth(real_db_client):
    r = real_db_client.get("/v1/fundamentals/financials?exchange=NSE")
    assert r.status_code == 401


# ============================================================
# Phase 4A — market cap endpoints
# ============================================================


from trip_ema.persistence.fundamentals_repo import DailyMarketCapRepository


class TestListMarketCap:

    def test_returns_empty_for_date_with_no_rows(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/market-cap?trade_date=2026-05-22",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["rows"] == []

    def test_returns_sorted_by_mcap_desc(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        td = date(2026, 5, 22)
        with transaction(db_engine) as conn:
            DailyMarketCapRepository(conn).upsert_snapshot(
                reliance_ticker,
                trade_date=td, close_price=1450.0,
                shares_used=6_700_000_000,
                market_cap=9_715_000_000_000.0,
                source="COMPUTED",
            )
        r = real_db_client.get(
            "/v1/fundamentals/market-cap?trade_date=2026-05-22",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert r.status_code == 200
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["ticker_id"] == reliance_ticker
        assert row["close_price"] == 1450.0
        assert row["market_cap"] == 9_715_000_000_000.0
        assert row["source"] == "COMPUTED"

    def test_limit_param_truncates_results(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        td = date(2026, 5, 22)
        with transaction(db_engine) as conn:
            DailyMarketCapRepository(conn).upsert_snapshot(
                reliance_ticker,
                trade_date=td, close_price=1.0, shares_used=1,
                market_cap=1.0, source="COMPUTED",
            )
        r = real_db_client.get(
            "/v1/fundamentals/market-cap?trade_date=2026-05-22&limit=1",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200

    def test_exchange_filter(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        td = date(2026, 5, 22)
        with transaction(db_engine) as conn:
            DailyMarketCapRepository(conn).upsert_snapshot(
                reliance_ticker,
                trade_date=td, close_price=1.0, shares_used=1,
                market_cap=1.0, source="COMPUTED",
            )
        r = real_db_client.get(
            "/v1/fundamentals/market-cap?trade_date=2026-05-22&exchange=NSE",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["exchange"] == "NSE"
        assert len(body["rows"]) == 1

    def test_rejects_invalid_exchange(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/market-cap?trade_date=2026-05-22&exchange=BOGUS",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400


class TestGetMarketCapHistory:

    def test_returns_history_newest_first(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        with transaction(db_engine) as conn:
            r = DailyMarketCapRepository(conn)
            for td_iso, mc in [
                (date(2026, 3, 1),  9_000_000_000_000.0),
                (date(2026, 4, 1),  9_500_000_000_000.0),
                (date(2026, 5, 22), 9_715_000_000_000.0),
            ]:
                r.upsert_snapshot(
                    reliance_ticker, trade_date=td_iso,
                    close_price=1.0, shares_used=1,
                    market_cap=mc, source="COMPUTED",
                )
        r = real_db_client.get(
            f"/v1/fundamentals/market-cap/{reliance_ticker}/history",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert r.status_code == 200
        dates_returned = [row["trade_date"] for row in body["rows"]]
        # Newest first.
        assert dates_returned == ["2026-05-22", "2026-04-01", "2026-03-01"]

    def test_returns_empty_history_for_unknown(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/market-cap/999999/history",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["rows"] == []


class TestComputeMarketCapEndpoint:

    def test_returns_202_and_kicks_runner(
        self, real_db_client, db_engine, reliance_ticker, monkeypatch,
    ):
        called = []
        def fake_runner(engine, exchange, *, from_date, to_date,
                        force=False, **kw):
            called.append((exchange, from_date, to_date, force))
            return 0
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_market_cap_backfill",
            fake_runner,
        )
        r = real_db_client.post(
            "/v1/fundamentals/compute-market-cap?exchange=NSE"
            "&backfill_from=2025-05-22&backfill_to=2026-05-22&force=true",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 202
        body = r.json()
        assert body["action"] == "compute-market-cap"
        assert called == [
            ("NSE", date(2025, 5, 22), date(2026, 5, 22), True),
        ]

    def test_rejects_inverted_date_range(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/compute-market-cap?exchange=NSE"
            "&backfill_from=2026-05-22&backfill_to=2025-05-22",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400

    def test_rejects_missing_backfill_from(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/compute-market-cap?exchange=NSE",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400

    def test_rejects_invalid_exchange(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/compute-market-cap?exchange=BOGUS"
            "&backfill_from=2025-05-22",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400


def test_market_cap_endpoints_require_auth(real_db_client):
    r = real_db_client.get(
        "/v1/fundamentals/market-cap?trade_date=2026-05-22"
    )
    assert r.status_code == 401


# ============================================================
# Phase 4B-3 — industry index endpoints
# ============================================================


from trip_ema.persistence.fundamentals_repo import (
    IndustryIndexConstituentRepository,
    IndustryIndexRepository,
    IndustryIndexValueRepository,
)


# ---------- helpers ---------- #


def _create_index(db_engine, industry="IT - Services",
                  sector="Technology",
                  base_date=date(2025, 10, 1)):
    with transaction(db_engine) as conn:
        IndustryIndexRepository(conn).upsert(
            industry, sector_name=sector, base_date=base_date,
        )


# ---------- GET /industry-indices ---------- #


class TestListIndustryIndices:

    def test_returns_empty_when_no_indices(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json() == {"rows": []}

    def test_returns_active_indices(self, real_db_client, db_engine):
        _create_index(db_engine, "IT - Services")
        _create_index(db_engine, "Banks - Regional", sector="Financials")
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        names = {row["industry_name"] for row in r.json()["rows"]}
        assert names == {"IT - Services", "Banks - Regional"}

    def test_excludes_retired_by_default(self, real_db_client, db_engine):
        _create_index(db_engine, "IT - Services")
        _create_index(db_engine, "Old Industry")
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).set_status(
                "Old Industry", "RETIRED",
            )
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices",
            headers=AUTH_HEADERS,
        )
        names = {row["industry_name"] for row in r.json()["rows"]}
        assert names == {"IT - Services"}

    def test_include_retired_param(self, real_db_client, db_engine):
        _create_index(db_engine, "IT - Services")
        _create_index(db_engine, "Old Industry")
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).set_status(
                "Old Industry", "RETIRED",
            )
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices?include_retired=true",
            headers=AUTH_HEADERS,
        )
        names = {row["industry_name"] for row in r.json()["rows"]}
        assert names == {"IT - Services", "Old Industry"}

    def test_requires_auth(self, real_db_client):
        r = real_db_client.get("/v1/fundamentals/industry-indices")
        assert r.status_code == 401


# ---------- GET /industry-indices/{name} ---------- #


class TestGetIndustryIndex:

    def test_returns_404_for_unknown(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices/Nonexistent",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 404

    def test_returns_known_index(self, real_db_client, db_engine):
        _create_index(db_engine, "IT - Services")
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices/IT - Services",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["industry_name"] == "IT - Services"
        assert body["base_value"] == 1000.0
        assert body["status"] == "ACTIVE"
        assert body["methodology"] == "MODIFIED_LASPEYRES"


# ---------- GET /industry-indices/{name}/constituents ---------- #


class TestGetIndustryIndexConstituents:

    def _setup_with_constituents(self, db_engine, reliance_ticker):
        _create_index(db_engine, "IT - Services")
        with transaction(db_engine) as conn:
            IndustryIndexConstituentRepository(conn).insert_constituent(
                "IT - Services",
                ticker_id=reliance_ticker,
                rebalance_date=date(2025, 10, 1),
                weight_at_rebalance=1.0,
                market_cap_at_rebalance=1e12,
                close_at_rebalance=1500.0,
            )

    def test_returns_active_constituents(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        self._setup_with_constituents(db_engine, reliance_ticker)
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices/IT - Services/constituents",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["industry_name"] == "IT - Services"
        assert body["as_of"] is None
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["ticker_id"] == reliance_ticker
        assert row["weight_at_rebalance"] == 1.0
        assert row["effective_to"] is None

    def test_as_of_parameter(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        self._setup_with_constituents(db_engine, reliance_ticker)
        # Trade date within the effective period.
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices/IT - Services/constituents"
            "?as_of=2025-12-01",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["as_of"] == "2025-12-01"
        assert len(body["rows"]) == 1

    def test_as_of_before_effective_returns_empty(
        self, real_db_client, db_engine, reliance_ticker,
    ):
        self._setup_with_constituents(db_engine, reliance_ticker)
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices/IT - Services/constituents"
            "?as_of=2025-01-01",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert body["rows"] == []


# ---------- GET /industry-indices/{name}/values ---------- #


class TestGetIndustryIndexValues:

    def _setup_with_values(self, db_engine):
        _create_index(db_engine, "IT - Services")
        with transaction(db_engine) as conn:
            r = IndustryIndexValueRepository(conn)
            for td, v in [
                (date(2025, 10, 1), 1000.0),
                (date(2025, 10, 2), 1010.0),
                (date(2025, 10, 3), 1020.0),
            ]:
                r.upsert_value(
                    "IT - Services", trade_date=td, value=v,
                    constituents_used=10, constituents_missing=0,
                    renormalized=False,
                    effective_rebalance_date=date(2025, 10, 1),
                )

    def test_returns_history_newest_first(
        self, real_db_client, db_engine,
    ):
        self._setup_with_values(db_engine)
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices/IT - Services/values",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        rows = r.json()["rows"]
        dates = [r["trade_date"] for r in rows]
        assert dates == ["2025-10-03", "2025-10-02", "2025-10-01"]
        # Values match.
        assert rows[0]["value"] == 1020.0
        assert rows[2]["value"] == 1000.0

    def test_date_range_filter(self, real_db_client, db_engine):
        self._setup_with_values(db_engine)
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices/IT - Services/values"
            "?from_date=2025-10-02&to_date=2025-10-02",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["trade_date"] == "2025-10-02"
        assert body["from_date"] == "2025-10-02"

    def test_limit_param(self, real_db_client, db_engine):
        self._setup_with_values(db_engine)
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices/IT - Services/values?limit=2",
            headers=AUTH_HEADERS,
        )
        body = r.json()
        assert len(body["rows"]) == 2

    def test_unknown_index_returns_empty(self, real_db_client):
        r = real_db_client.get(
            "/v1/fundamentals/industry-indices/Nonexistent/values",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["rows"] == []


# ---------- POST /build-industry-indices ---------- #


class TestBuildIndustryIndicesEndpoint:

    def test_returns_202_and_kicks_runner(
        self, real_db_client, db_engine, monkeypatch,
    ):
        called = []
        def fake_build(engine, exchange, *, min_constituents, n, base_value):
            called.append((exchange, min_constituents, n, base_value))
            return {
                "qualified": [], "unqualified": [],
                "indexed": [], "failed": [],
            }
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_industry_index_build",
            fake_build,
        )
        r = real_db_client.post(
            "/v1/fundamentals/build-industry-indices?exchange=NSE"
            "&min_constituents=10&n=10&base_value=1000.0",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 202
        body = r.json()
        assert body["action"] == "build-industry-indices"
        # The synchronous executor in the test fixture ran the task.
        assert called == [("NSE", 10, 10, 1000.0)]

    def test_rejects_invalid_exchange(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/build-industry-indices?exchange=BOGUS",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400

    def test_requires_auth(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/build-industry-indices?exchange=NSE"
        )
        assert r.status_code == 401


# ---------- POST /compute-index-values ---------- #


class TestComputeIndexValuesEndpoint:

    def test_rejects_no_industry_and_no_all_active(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/compute-index-values",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400
        assert "industry" in r.json()["detail"]

    def test_rejects_both_industry_and_all_active(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/compute-index-values"
            "?industry=IT - Services&all_active=true",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400

    def test_rejects_inverted_date_range(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/compute-index-values"
            "?industry=IT - Services"
            "&from_date=2026-05-01&to_date=2025-10-01",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400

    def test_returns_202_for_industry(
        self, real_db_client, db_engine, monkeypatch,
    ):
        _create_index(db_engine, "IT - Services")
        called = []
        def fake_backfill(engine, industry_name, **kw):
            called.append((industry_name, kw))
            return 1
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_index_value_backfill",
            fake_backfill,
        )
        r = real_db_client.post(
            "/v1/fundamentals/compute-index-values"
            "?industry=IT - Services&from_date=2025-10-01",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 202
        # Sync executor invoked the runner.
        assert len(called) == 1
        assert called[0][0] == "IT - Services"

    def test_returns_202_for_all_active(
        self, real_db_client, db_engine, monkeypatch,
    ):
        _create_index(db_engine, "IT - Services")
        _create_index(db_engine, "Banks - Regional", sector="Financials")
        called = []
        def fake_backfill(engine, industry_name, **kw):
            called.append(industry_name)
            return 1
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_index_value_backfill",
            fake_backfill,
        )
        r = real_db_client.post(
            "/v1/fundamentals/compute-index-values?all_active=true",
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 202
        # Both indices got backfilled.
        assert set(called) == {"IT - Services", "Banks - Regional"}

    def test_requires_auth(self, real_db_client):
        r = real_db_client.post(
            "/v1/fundamentals/compute-index-values?all_active=true"
        )
        assert r.status_code == 401


# ============================================================
# Phase 5-4 — indicator state endpoints
# ============================================================


# === STEP 14A-FIXUP: legacy indicator/signal endpoint tests truncated ===
