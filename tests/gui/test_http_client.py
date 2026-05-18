"""Tests for the trip_ema GUI HTTP client. No Qt — pure requests-mocking."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from trip_ema.gui.http_client import (
    ApiError, HttpClientConfig, TripEmaHttpClient,
)


def _build_client(token: str | None = "test-token") -> TripEmaHttpClient:
    return TripEmaHttpClient(HttpClientConfig(
        base_url="http://localhost:8000", token=token,
    ))


class TestTokenAuth:

    def test_token_in_session_headers_when_set(self):
        c = _build_client(token="hunter2")
        # The bearer-token header should be installed on the session
        assert c._session.headers.get("Authorization") == "Bearer hunter2"
        assert c.token_configured

    def test_no_token_means_no_auth_header(self):
        c = _build_client(token=None)
        assert "Authorization" not in c._session.headers
        assert not c.token_configured


class TestSuccessfulRequests:

    def test_get_etl_status(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"recent_runs": [], "parked_count": 0}
            mock_req.return_value = resp
            result = c.get_etl_status(limit=10)
            assert result["parked_count"] == 0
            # Verify GET method + correct URL + params
            args, kwargs = mock_req.call_args
            assert args[0] == "GET"
            assert "/v1/admin/etl/status" in args[1]
            assert kwargs["params"] == {"limit": 10}

    def test_run_eod(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "action": "run-eod", "run_id": None,
                "accepted_at": "2026-05-17T10:00:00Z", "notes": "ok",
            }
            mock_req.return_value = resp
            result = c.run_eod(lookback_days=14)
            assert result["action"] == "run-eod"
            args, kwargs = mock_req.call_args
            assert args[0] == "POST"
            assert kwargs["params"] == {"lookback_days": 14}

    def test_backfill_includes_tier_when_set(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"action": "backfill", "run_id": None,
                                     "accepted_at": "2026-05-17T10:00:00Z",
                                     "notes": "ok"}
            mock_req.return_value = resp
            c.backfill(start_date=date(2020, 1, 1), tier=1)
            _, kwargs = mock_req.call_args
            assert kwargs["params"]["start_date"] == "2020-01-01"
            assert kwargs["params"]["tier"] == 1

    def test_backfill_omits_tier_when_none(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"action": "backfill", "run_id": None,
                                     "accepted_at": "2026-05-17T10:00:00Z",
                                     "notes": "ok"}
            mock_req.return_value = resp
            c.backfill(start_date=date(2020, 1, 1), tier=None)
            _, kwargs = mock_req.call_args
            assert "tier" not in kwargs["params"]

    def test_reprime_ticker(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"succeeded": True, "bars_loaded": 100}
            mock_req.return_value = resp
            c.reprime_ticker("NSE", "RELIANCE")
            args, _ = mock_req.call_args
            assert "/v1/admin/reprime-ticker/NSE/RELIANCE" in args[1]

    def test_get_signal_history(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "exchange": "NSE", "symbol": "RELIANCE", "instrument": "EQ",
                "timeframe": "1W",
                "bars": [], "signal_events": [],
                "warmup_bars_count": 0, "notes": "ok",
            }
            mock_req.return_value = resp
            result = c.get_signal_history("NSE", "RELIANCE", years=5)
            assert result["symbol"] == "RELIANCE"
            args, kwargs = mock_req.call_args
            assert args[0] == "GET"
            assert "/v1/signals/NSE/RELIANCE/history" in args[1]
            assert kwargs["params"]["years"] == 5
            assert kwargs["params"]["instrument"] == "EQ"


class TestErrorHandling:

    def test_http_4xx_raises_api_error_with_detail(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 401
            resp.json.return_value = {"detail": "Missing Authorization header"}
            mock_req.return_value = resp
            with pytest.raises(ApiError) as ei:
                c.refresh_listings()
            assert ei.value.status_code == 401
            assert "Missing Authorization" in ei.value.message

    def test_http_503_raises_api_error(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 503
            resp.json.return_value = {"detail": "ETL admin endpoints disabled"}
            mock_req.return_value = resp
            with pytest.raises(ApiError) as ei:
                c.run_eod()
            assert ei.value.status_code == 503

    def test_network_failure_raises_api_error_with_status_0(self):
        import requests
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            mock_req.side_effect = requests.ConnectionError("connection refused")
            with pytest.raises(ApiError) as ei:
                c.get_etl_status()
            assert ei.value.status_code == 0
            assert "connection refused" in ei.value.message

    def test_invalid_json_response_raises_api_error(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.side_effect = ValueError("not json")
            mock_req.return_value = resp
            with pytest.raises(ApiError) as ei:
                c.get_etl_status()
            assert "invalid JSON" in ei.value.message

    def test_error_response_without_detail_falls_back_to_body(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 500
            resp.json.side_effect = ValueError("no json")
            resp.text = "Internal Server Error"
            mock_req.return_value = resp
            with pytest.raises(ApiError) as ei:
                c.run_eod()
            assert ei.value.status_code == 500
            # Body included in message
            assert "Internal" in ei.value.message
