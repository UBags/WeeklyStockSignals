"""Tests for the trip_ema GUI HTTP client. No Qt — pure requests-mocking.

Phase 14.6a: every ETL-action method now sends `exchange=` in the
query params. Default is `"NSE"` at the client layer for backward
compatibility; the underlying server requires it.
"""

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

    def test_get_etl_status_without_exchange(self):
        """Backward compat: omitting exchange sends only {limit: 10}.
        The server falls back to config.etl.exchange in that case."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"recent_runs": [], "parked_count": 0}
            mock_req.return_value = resp
            result = c.get_etl_status(limit=10)
            assert result["parked_count"] == 0
            args, kwargs = mock_req.call_args
            assert args[0] == "GET"
            assert "/v1/admin/etl/status" in args[1]
            assert kwargs["params"] == {"limit": 10}

    def test_get_etl_status_with_exchange(self):
        """Phase 14.6c: when exchange is passed, it goes through as a
        query param so the server can scope tier_counts."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"recent_runs": [], "parked_count": 0}
            mock_req.return_value = resp
            c.get_etl_status(limit=20, exchange="BSE")
            _, kwargs = mock_req.call_args
            assert kwargs["params"]["limit"] == 20
            assert kwargs["params"]["exchange"] == "BSE"

    def test_get_etl_status_none_exchange_omits_param(self):
        """exchange=None must NOT send `exchange=` (would 422 server-
        side as empty string). It should send no exchange key at all."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"recent_runs": [], "parked_count": 0}
            mock_req.return_value = resp
            c.get_etl_status(limit=10, exchange=None)
            _, kwargs = mock_req.call_args
            assert "exchange" not in kwargs["params"]

    def test_list_exchanges(self):
        """Phase 14.6c: list_exchanges hits /v1/admin/exchanges (GET,
        no params) and returns the parsed payload verbatim."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "exchanges": ["BSE", "NSE", "NYSE"],
                "total": 3,
                "snapshot_taken_at": "2026-05-21T10:00:00Z",
            }
            mock_req.return_value = resp
            result = c.list_exchanges()
            assert result["exchanges"] == ["BSE", "NSE", "NYSE"]
            args, kwargs = mock_req.call_args
            assert args[0] == "GET"
            assert "/v1/admin/exchanges" in args[1]

    def test_get_exchange_state(self):
        """Phase 14.6c: get_exchange_state(NSE) → GET
        /v1/admin/exchange-state/NSE."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "exchange": "NSE",
                "last_eod_outcome": "success",
                "last_eod_data_date": "2026-05-19",
            }
            mock_req.return_value = resp
            result = c.get_exchange_state("NSE")
            assert result["exchange"] == "NSE"
            args, _ = mock_req.call_args
            assert "/v1/admin/exchange-state/NSE" in args[1]

    def test_get_exchange_state_404_raises_api_error(self):
        """A 404 (exchange has no activity yet) flows through as
        ApiError(404), not silent None — the caller decides whether
        that's an error or an empty-state signal."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 404
            resp.json.return_value = {
                "detail": "No exchange_state row for exchange='BSE'.",
            }
            mock_req.return_value = resp
            with pytest.raises(ApiError) as ei:
                c.get_exchange_state("BSE")
            assert ei.value.status_code == 404
            assert "BSE" in ei.value.message

    def test_list_exchange_states(self):
        """Phase 14.6c: list_exchange_states → GET /v1/admin/exchange-state."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "exchanges": [
                    {"exchange": "NSE", "last_eod_outcome": "success"},
                ],
                "total": 1,
                "snapshot_taken_at": "2026-05-21T10:00:00Z",
            }
            mock_req.return_value = resp
            result = c.list_exchange_states()
            assert result["total"] == 1
            args, _ = mock_req.call_args
            assert args[0] == "GET"
            assert "/v1/admin/exchange-state" in args[1]

    def test_run_eod_default_exchange(self):
        """run_eod() with no exchange kwarg defaults to NSE."""
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
            assert kwargs["params"]["exchange"] == "NSE"
            assert kwargs["params"]["lookback_days"] == 14

    def test_run_eod_explicit_exchange(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "action": "run-eod", "run_id": None,
                "accepted_at": "2026-05-17T10:00:00Z", "notes": "ok",
            }
            mock_req.return_value = resp
            c.run_eod(exchange="BSE", lookback_days=14)
            _, kwargs = mock_req.call_args
            assert kwargs["params"]["exchange"] == "BSE"

    def test_refresh_listings_sends_exchange(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "action": "refresh-listings", "run_id": None,
                "accepted_at": "2026-05-17T10:00:00Z", "notes": "ok",
            }
            mock_req.return_value = resp
            c.refresh_listings()
            _, kwargs = mock_req.call_args
            assert kwargs["params"] == {"exchange": "NSE"}

    def test_run_intraday_sends_exchange(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "action": "run-intraday", "run_id": None,
                "accepted_at": "2026-05-17T10:00:00Z", "notes": "ok",
            }
            mock_req.return_value = resp
            c.run_intraday(exchange="NSE")
            _, kwargs = mock_req.call_args
            assert kwargs["params"] == {"exchange": "NSE"}

    def test_recompute_tiers_sends_exchange(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "action": "recompute-tiers", "run_id": None,
                "accepted_at": "2026-05-17T10:00:00Z", "notes": "ok",
            }
            mock_req.return_value = resp
            c.recompute_tiers()
            _, kwargs = mock_req.call_args
            assert kwargs["params"] == {"exchange": "NSE"}

    def test_unpark_all_sends_exchange(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "action": "unpark-all", "run_id": None,
                "accepted_at": "2026-05-17T10:00:00Z", "notes": "ok",
            }
            mock_req.return_value = resp
            c.unpark_all()
            _, kwargs = mock_req.call_args
            assert kwargs["params"] == {"exchange": "NSE"}

    def test_backfill_includes_tier_when_set(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "action": "backfill", "run_id": None,
                "accepted_at": "2026-05-17T10:00:00Z", "notes": "ok",
            }
            mock_req.return_value = resp
            c.backfill(start_date=date(2020, 1, 1), tier=1)
            _, kwargs = mock_req.call_args
            assert kwargs["params"]["exchange"] == "NSE"
            assert kwargs["params"]["start_date"] == "2020-01-01"
            assert kwargs["params"]["tier"] == 1

    def test_backfill_omits_tier_when_none(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "action": "backfill", "run_id": None,
                "accepted_at": "2026-05-17T10:00:00Z", "notes": "ok",
            }
            mock_req.return_value = resp
            c.backfill(start_date=date(2020, 1, 1), tier=None)
            _, kwargs = mock_req.call_args
            assert kwargs["params"]["exchange"] == "NSE"
            assert "tier" not in kwargs["params"]

    def test_backfill_with_explicit_exchange(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "action": "backfill", "run_id": None,
                "accepted_at": "2026-05-17T10:00:00Z", "notes": "ok",
            }
            mock_req.return_value = resp
            c.backfill(start_date=date(2020, 1, 1), exchange="BSE")
            _, kwargs = mock_req.call_args
            assert kwargs["params"]["exchange"] == "BSE"

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


# ============================================================
# Phase 15b: signal-generation admin endpoints
# ============================================================


class TestGenerateSignals:

    def test_default_timeframe_is_omitted_so_server_uses_1W_default(self):
        """Calling generate_signals(exchange='NSE') with no timeframes
        should NOT send a `timeframe` param — the server-side
        Query(default=['1W']) populates it instead. This keeps the
        client/server contracts aligned: 'no choice expressed' is
        not the same as 'choose 1W explicitly'."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 202
            resp.json.return_value = {
                "run_id": 1, "exchange": "NSE", "timeframes": ["1W"],
                "force_full": False,
                "estimated_output_path": "generated_signals/NSE.json",
                "submitted_at": "2026-05-21T01:00:00+00:00",
            }
            mock_req.return_value = resp
            c.generate_signals(exchange="NSE")
            _, kwargs = mock_req.call_args
            assert kwargs["params"]["exchange"] == "NSE"
            assert kwargs["params"]["force_full"] is False
            assert "timeframe" not in kwargs["params"]

    def test_explicit_timeframes_pass_as_list(self):
        """The requests library turns {'timeframe': ['1D','1W']} into
        ?timeframe=1D&timeframe=1W on the wire, which matches the
        FastAPI Query(default=['1W']) shape. Verify our caller hands
        a list (not a comma-joined string)."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 202
            resp.json.return_value = {
                "run_id": 2, "exchange": "NSE", "timeframes": ["1D", "1W"],
                "force_full": False,
                "estimated_output_path": "generated_signals/NSE.json",
                "submitted_at": "2026-05-21T01:00:00+00:00",
            }
            mock_req.return_value = resp
            c.generate_signals(exchange="NSE", timeframes=["1D", "1W"])
            _, kwargs = mock_req.call_args
            assert kwargs["params"]["timeframe"] == ["1D", "1W"]

    def test_force_full_propagates(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 202
            resp.json.return_value = {
                "run_id": 3, "exchange": "NSE", "timeframes": ["1W"],
                "force_full": True,
                "estimated_output_path": "generated_signals/NSE.json",
                "submitted_at": "2026-05-21T01:00:00+00:00",
            }
            mock_req.return_value = resp
            c.generate_signals(exchange="NSE", force_full=True)
            _, kwargs = mock_req.call_args
            assert kwargs["params"]["force_full"] is True

    def test_409_raises_api_error_with_detail(self):
        """The concurrent-run guard returns 409 with a `detail`
        string that mentions the existing run_id. The client should
        raise ApiError(409, detail) so the GUI can show the operator
        a friendly message."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 409
            resp.json.return_value = {
                "detail": (
                    "A signal_backtest run is already in flight for "
                    "exchange='NSE' (run_id=15). Wait for completion."
                ),
            }
            mock_req.return_value = resp
            with pytest.raises(ApiError) as ei:
                c.generate_signals(exchange="NSE")
            assert ei.value.status_code == 409
            assert "run_id=15" in ei.value.message


class TestGetSignalGenerationMetadata:

    def test_file_exists_returns_full_metadata(self):
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "exchange": "NSE", "exists": True,
                "filename": "NSE.json",
                "size_bytes": 14_680_064,
                "mtime": "2026-05-21T01:42:00+00:00",
                "generated_by_run_id": 17,
                "schema_version": "1.0",
                "counts": {
                    "tickers": 2283, "signals_total": 105684,
                    "signals_by_type": {
                        "BUY": 32987, "SELL": 35396,
                        "EXIT_FROM_BUY": 17857, "EXIT_FROM_SELL": 19444,
                    },
                    "first_date": "2005-01-03", "last_date": "2026-05-15",
                },
            }
            mock_req.return_value = resp
            result = c.get_signal_generation_metadata("NSE")
            assert result["exists"] is True
            assert result["size_bytes"] == 14_680_064
            assert result["counts"]["signals_total"] == 105684
            _, kwargs = mock_req.call_args
            assert kwargs["params"] == {"exchange": "NSE"}

    def test_file_missing_returns_exists_false(self):
        """A configured exchange with no /generate run yet returns
        200 with exists=false (NOT 404). Other fields are None."""
        c = _build_client()
        with patch.object(c._session, "request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "exchange": "NSE", "exists": False,
                "filename": None, "size_bytes": None, "mtime": None,
                "generated_by_run_id": None, "schema_version": None,
                "counts": None,
            }
            mock_req.return_value = resp
            result = c.get_signal_generation_metadata("NSE")
            assert result["exists"] is False
            assert result["size_bytes"] is None
