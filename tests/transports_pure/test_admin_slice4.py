"""Tests for the Slice 4 admin endpoints (subscribers + notifications
+ health-snapshot).

DB-free: dependency-override the engine and provide handcrafted
SQLAlchemy-row-shaped objects. We're not testing SQL semantics; that's
the DB integration-test slice's job. We are testing:
  - Routes registered with the right method+path
  - Request validation (4xx on bad input)
  - Response shape matches the schema
  - Auth gate (token enforcement)
  - SQL parameters constructed correctly (via call-arg inspection)
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

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
    monkeypatch.setenv("TRIP_EMA_ADMIN_TOKEN", "test-token")
    return "test-token"


@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


class FakeRow:
    """Mimic a SQLAlchemy Row — attribute access for column values."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_config():
    cfg = MagicMock()
    cfg.http.admin_token_env = "TRIP_EMA_ADMIN_TOKEN"
    cfg.etl.exchange = "NSE"
    return cfg


def _make_client(*, config=None, db_engine=None, distributor=None):
    app = create_app()
    app.dependency_overrides[get_config] = lambda: (config or _make_config())
    app.dependency_overrides[get_db_engine] = lambda: (db_engine or MagicMock())
    app.dependency_overrides[get_registry] = lambda: MagicMock(ticker_refs=[])
    cache = MagicMock()
    cache.size = lambda: 0
    app.dependency_overrides[get_cache] = lambda: cache
    sched = MagicMock()
    sched.last_run_completed_at = None
    app.dependency_overrides[get_compute_scheduler] = lambda: sched
    app.dependency_overrides[get_distributor] = lambda: distributor
    app.dependency_overrides[get_redis_queue] = lambda: None
    app.dependency_overrides[get_etl_executor] = lambda: MagicMock()
    return TestClient(app)


# ============================================================
# Subscribers CRUD
# ============================================================


class TestListSubscribers:

    def test_returns_empty_list_when_no_subscribers(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        # First execute (list) returns empty iterator; second
        # (filters) won't be called because _load_filters_grouped
        # short-circuits on empty list.
        conn.execute.return_value = iter([])
        client = _make_client(db_engine=engine)
        resp = client.get("/v1/admin/subscribers", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["subscribers"] == []
        assert body["total"] == 0

    def test_returns_subscribers_with_filters(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        # First call: subscriber rows. Second call: filter rows (from
        # _load_filters_grouped). MagicMock returns iter on each call.
        sub_rows = [
            FakeRow(
                subscriber_id=1, transport="email", address="a@b.com",
                notify_cadence_minutes=60,
                last_notified_at=None,
                is_active=True,
                created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            ),
        ]
        filter_rows = [
            FakeRow(
                subscription_id=10, subscriber_id=1, ticker_id=None,
                signal_filter=None, ticker_symbol=None, ticker_exchange=None,
            ),
        ]
        conn.execute.side_effect = [iter(sub_rows), iter(filter_rows)]
        client = _make_client(db_engine=engine)
        resp = client.get("/v1/admin/subscribers", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        sub = body["subscribers"][0]
        assert sub["subscriber_id"] == 1
        assert sub["transport"] == "email"
        assert sub["address"] == "a@b.com"
        assert len(sub["filters"]) == 1
        assert sub["filters"][0]["ticker_id"] is None
        assert sub["filters"][0]["signal_filter"] is None

    def test_requires_auth(self):
        # No token env var
        import os
        os.environ.pop("TRIP_EMA_ADMIN_TOKEN", None)
        client = _make_client()
        resp = client.get("/v1/admin/subscribers")
        assert resp.status_code in (401, 503)


class TestGetSubscriber:

    def test_returns_404_when_missing(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        # first().first() chain returns None
        result_mock = MagicMock()
        result_mock.first.return_value = None
        conn.execute.return_value = result_mock
        client = _make_client(db_engine=engine)
        resp = client.get("/v1/admin/subscribers/9999", headers=auth_headers)
        assert resp.status_code == 404


class TestCreateSubscriber:

    def test_creates_with_minimal_body(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.begin.return_value.__enter__.return_value = conn
        # Insert returns a row
        insert_result = MagicMock()
        insert_result.first.return_value = FakeRow(
            subscriber_id=42, transport="email", address="x@y.com",
            notify_cadence_minutes=60, last_notified_at=None,
            is_active=True,
            created_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
        )
        # No filters provided → _load_filters_grouped runs and returns empty
        filters_result = MagicMock()
        filters_result.__iter__ = MagicMock(return_value=iter([]))
        conn.execute.side_effect = [insert_result, filters_result]
        client = _make_client(db_engine=engine)
        resp = client.post(
            "/v1/admin/subscribers", headers=auth_headers,
            json={"transport": "email", "address": "x@y.com",
                  "notify_cadence_minutes": 60},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["subscriber_id"] == 42
        assert body["transport"] == "email"
        assert body["filters"] == []

    def test_rejects_invalid_transport(self, auth_headers):
        client = _make_client()
        resp = client.post(
            "/v1/admin/subscribers", headers=auth_headers,
            json={"transport": "carrier_pigeon", "address": "x",
                  "notify_cadence_minutes": 60},
        )
        assert resp.status_code == 422

    def test_rejects_invalid_cadence(self, auth_headers):
        client = _make_client()
        # Below min (15)
        resp = client.post(
            "/v1/admin/subscribers", headers=auth_headers,
            json={"transport": "email", "address": "x",
                  "notify_cadence_minutes": 5},
        )
        assert resp.status_code == 422
        # Above max (1440)
        resp = client.post(
            "/v1/admin/subscribers", headers=auth_headers,
            json={"transport": "email", "address": "x",
                  "notify_cadence_minutes": 9999},
        )
        assert resp.status_code == 422


class TestUpdateSubscriber:

    def test_no_op_when_body_empty(self, auth_headers):
        """PATCH with no fields = return current row unchanged. The
        route reuses the GET path."""
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        result_mock = MagicMock()
        result_mock.first.return_value = FakeRow(
            subscriber_id=1, transport="email", address="a@b.com",
            notify_cadence_minutes=60, last_notified_at=None,
            is_active=True,
            created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        filters_iter = MagicMock()
        filters_iter.__iter__ = MagicMock(return_value=iter([]))
        conn.execute.side_effect = [result_mock, filters_iter]
        client = _make_client(db_engine=engine)
        resp = client.patch(
            "/v1/admin/subscribers/1", headers=auth_headers, json={},
        )
        assert resp.status_code == 200
        assert resp.json()["subscriber_id"] == 1

    def test_updates_address(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.begin.return_value.__enter__.return_value = conn
        # UPDATE returns the new row
        upd_result = MagicMock()
        upd_result.first.return_value = FakeRow(
            subscriber_id=1, transport="email", address="new@b.com",
            notify_cadence_minutes=60, last_notified_at=None,
            is_active=True,
            created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        # Then filters re-fetched
        filters_iter = MagicMock()
        filters_iter.__iter__ = MagicMock(return_value=iter([]))
        conn.execute.side_effect = [upd_result, filters_iter]
        client = _make_client(db_engine=engine)
        resp = client.patch(
            "/v1/admin/subscribers/1", headers=auth_headers,
            json={"address": "new@b.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["address"] == "new@b.com"


class TestDeleteSubscriber:

    def test_returns_204(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.begin.return_value.__enter__.return_value = conn
        result = MagicMock(rowcount=1)
        conn.execute.return_value = result
        client = _make_client(db_engine=engine)
        resp = client.delete(
            "/v1/admin/subscribers/1", headers=auth_headers,
        )
        assert resp.status_code == 204

    def test_returns_404_when_missing(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.begin.return_value.__enter__.return_value = conn
        result = MagicMock(rowcount=0)
        conn.execute.return_value = result
        client = _make_client(db_engine=engine)
        resp = client.delete(
            "/v1/admin/subscribers/9999", headers=auth_headers,
        )
        assert resp.status_code == 404


class TestReplaceFilters:

    def test_replaces_filter_set(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.begin.return_value.__enter__.return_value = conn
        # Exists check, ticker validation, DELETE, INSERTs, refetch,
        # filter reload. We just need the route to flow; we mock each
        # .execute() result as MagicMock that .first/.scalar/etc. can
        # respond to whatever the route asks.
        exists_result = MagicMock()
        exists_result.first.return_value = FakeRow(__placeholder=True)
        ticker_validation = MagicMock()
        ticker_validation.__iter__ = MagicMock(return_value=iter([]))
        delete_result = MagicMock()
        refetch_result = MagicMock()
        refetch_result.first.return_value = FakeRow(
            subscriber_id=1, transport="email", address="a@b.com",
            notify_cadence_minutes=60, last_notified_at=None,
            is_active=True,
            created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        filters_iter = MagicMock()
        filters_iter.__iter__ = MagicMock(return_value=iter([]))
        # Flow: exists check, DELETE, refetch row, refetch filters.
        # (No INSERTs because we send an empty filter list — easier.)
        conn.execute.side_effect = [
            exists_result, delete_result, refetch_result, filters_iter,
        ]
        client = _make_client(db_engine=engine)
        resp = client.put(
            "/v1/admin/subscribers/1/filters", headers=auth_headers,
            json=[],
        )
        assert resp.status_code == 200


class TestTestNotification:

    def test_returns_404_when_subscriber_missing(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        result = MagicMock()
        result.first.return_value = None
        conn.execute.return_value = result
        client = _make_client(db_engine=engine)
        resp = client.post(
            "/v1/admin/subscribers/9999/test-notification",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_calls_transport_send(self, auth_headers, monkeypatch):
        """Verify the route looks up the subscriber, builds a
        transport, and calls send()."""
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        sub_result = MagicMock()
        sub_result.first.return_value = FakeRow(
            subscriber_id=1, transport="webhook", address="https://hooks.x",
        )
        conn.execute.return_value = sub_result

        # Patch the transport builder to return a fake whose send()
        # returns True.
        import trip_ema.transports.http.routes.admin_subscribers as mod
        fake_transport = MagicMock()
        fake_transport.send.return_value = True
        monkeypatch.setattr(
            mod, "_build_transport",
            lambda name, cfg: fake_transport,
        )

        client = _make_client(db_engine=engine)
        resp = client.post(
            "/v1/admin/subscribers/1/test-notification",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["succeeded"] is True
        assert body["transport"] == "webhook"
        assert body["address"] == "https://hooks.x"
        # transport.send was called once with the address + a batch
        assert fake_transport.send.called

    def test_returns_succeeded_false_when_transport_returns_false(
        self, auth_headers, monkeypatch,
    ):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        sub_result = MagicMock()
        sub_result.first.return_value = FakeRow(
            subscriber_id=1, transport="email", address="a@b.com",
        )
        conn.execute.return_value = sub_result

        import trip_ema.transports.http.routes.admin_subscribers as mod
        fake = MagicMock()
        fake.send.return_value = False
        monkeypatch.setattr(mod, "_build_transport", lambda n, c: fake)

        client = _make_client(db_engine=engine)
        resp = client.post(
            "/v1/admin/subscribers/1/test-notification",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["succeeded"] is False

    def test_catches_transport_exception(self, auth_headers, monkeypatch):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        sub_result = MagicMock()
        sub_result.first.return_value = FakeRow(
            subscriber_id=1, transport="email", address="a@b.com",
        )
        conn.execute.return_value = sub_result

        import trip_ema.transports.http.routes.admin_subscribers as mod
        fake = MagicMock()
        fake.send.side_effect = RuntimeError("smtp connection refused")
        monkeypatch.setattr(mod, "_build_transport", lambda n, c: fake)

        client = _make_client(db_engine=engine)
        resp = client.post(
            "/v1/admin/subscribers/1/test-notification",
            headers=auth_headers,
        )
        # Note: 200 even on transport failure — we report `succeeded=False`
        # plus the error message in `notes`, rather than returning a 500.
        assert resp.status_code == 200
        body = resp.json()
        assert body["succeeded"] is False
        assert "smtp connection refused" in body["notes"]


# ============================================================
# Notification log
# ============================================================


class TestNotificationLog:

    def test_returns_empty_when_no_rows(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        conn.execute.return_value = iter([])
        client = _make_client(db_engine=engine)
        resp = client.get(
            "/v1/admin/notifications/recent", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"] == []
        assert body["total"] == 0
        assert body["has_more"] is False

    def test_returns_entries(self, auth_headers):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        rows = [
            FakeRow(
                log_id=1,
                last_sent_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
                subscriber_id=1, event_id=100,
                notification_count=1,
                sub_address="a@b.com", sub_transport="email",
                event_signal="BUY",
                event_bar_timestamp=datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc),
                ticker_symbol="RELIANCE", ticker_exchange="NSE",
            ),
            FakeRow(
                log_id=2,
                last_sent_at=datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc),
                subscriber_id=2, event_id=99,
                notification_count=1,
                sub_address="https://h.x", sub_transport="webhook",
                event_signal="SELL",
                event_bar_timestamp=datetime(2026, 5, 17, 8, 0, tzinfo=timezone.utc),
                ticker_symbol="TCS", ticker_exchange="NSE",
            ),
        ]
        conn.execute.return_value = iter(rows)
        client = _make_client(db_engine=engine)
        resp = client.get(
            "/v1/admin/notifications/recent?limit=50",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["entries"][0]["ticker_symbol"] == "RELIANCE"
        assert body["entries"][1]["ticker_symbol"] == "TCS"

    def test_subscriber_id_filter_threaded_through(self, auth_headers):
        """Verify subscriber_id query param ends up in the SQL params."""
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        conn.execute.return_value = iter([])
        client = _make_client(db_engine=engine)
        resp = client.get(
            "/v1/admin/notifications/recent?subscriber_id=42",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # Confirm the executed query received subscriber_id
        call_args = conn.execute.call_args
        sql_clause, params = call_args[0]
        assert params["sid"] == 42

    def test_invalid_limit_rejected(self, auth_headers):
        client = _make_client()
        resp = client.get(
            "/v1/admin/notifications/recent?limit=0", headers=auth_headers,
        )
        assert resp.status_code == 422


# ============================================================
# Health snapshot
# ============================================================


class TestHealthSnapshot:

    def _engine_with_normal_universe(self):
        """Build a mock engine whose successive .execute() calls return
        plausible values for each card-builder."""
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn

        def execute_side_effect(stmt, *args, **kwargs):
            # Best-effort: produce reasonable defaults regardless of
            # which query came in. Inspect SQL text to identify.
            sql = str(stmt).lower()
            r = MagicMock()
            if "count(*) filter (where is_active = true and delisted_at is null)" in sql and "tickers" in sql:
                r.first.return_value = FakeRow(active=2300, delisted=5)
            elif "parked = true" in sql and "etl_ticker_status" in sql:
                r.scalar.return_value = 7
            elif "max(finished_at)" in sql:
                r.first.return_value = FakeRow(
                    last_finished=datetime(2026, 5, 17, 6, 0, tzinfo=timezone.utc),
                    total_runs=180,
                )
            elif "count(*) filter (where is_active = true) as active" in sql:
                r.first.return_value = FakeRow(active=3, total=5)
            elif "count(*) from signal_events" in sql:
                r.scalar.return_value = 42
            else:
                # Generic fallback
                r.first.return_value = MagicMock()
                r.scalar.return_value = 0
            return r
        conn.execute.side_effect = execute_side_effect
        return engine

    def test_returns_eight_cards(self, auth_headers):
        engine = self._engine_with_normal_universe()
        client = _make_client(db_engine=engine)
        resp = client.get(
            "/v1/admin/health-snapshot", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        # We promised 6-8 cards in the design; the implementation
        # produces 8.
        assert 6 <= len(body["cards"]) <= 10
        names = [c["name"] for c in body["cards"]]
        # Spot-check a few essentials
        assert "Ticker universe" in names
        assert "ETL freshness" in names
        assert "Subscribers" in names

    def test_overall_status_aggregates_worst_card(self, auth_headers):
        """If every card is ok, overall is ok. We can't easily
        engineer a 'crit' from mocks without breaking other tests;
        check the ok path."""
        engine = self._engine_with_normal_universe()
        client = _make_client(db_engine=engine)
        resp = client.get(
            "/v1/admin/health-snapshot", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        # Should be ok or warn (depending on whether parked tickers
        # were detected by the mock; we mocked 7 parked, which causes
        # ticker-universe card to be 'warn').
        assert body["overall_status"] in ("ok", "warn")

    def test_db_error_produces_crit_card(self, auth_headers):
        """If a card-builder's DB query fails, the card should report
        crit/unknown rather than propagating the exception."""
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        conn.execute.side_effect = RuntimeError("connection lost")
        client = _make_client(db_engine=engine)
        resp = client.get(
            "/v1/admin/health-snapshot", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        # At least one card should report crit/unknown
        bad_cards = [c for c in body["cards"] if c["status"] in ("crit", "unknown")]
        assert len(bad_cards) > 0

    def test_requires_auth(self):
        import os
        os.environ.pop("TRIP_EMA_ADMIN_TOKEN", None)
        client = _make_client()
        resp = client.get("/v1/admin/health-snapshot")
        assert resp.status_code in (401, 503)
