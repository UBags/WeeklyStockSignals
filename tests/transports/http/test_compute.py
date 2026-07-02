"""Tests for POST /v1/compute (function a, stateless)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC


def _bar_payload(day: int, close: float = 100.0) -> dict:
    """An OHLCVBarPayload as a JSON-safe dict."""
    return {
        "timestamp": (
            datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day)
        ).isoformat(),
        "open": close - 0.5,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 1000.0,
    }


def _compute_body(
    *,
    n_history: int = 80,
    verbose: bool = False,
    timeframe: str = "1W",
) -> dict:
    history = [_bar_payload(i, close=100 + i) for i in range(n_history)]
    latest = _bar_payload(n_history, close=100 + n_history)
    return {
        "exchange": "NSE",
        "symbol": "NIFTY",
        "instrument": "INDEX",
        "timeframe": timeframe,
        "verbose": verbose,
        "history": history,
        "latest_bar": latest,
    }


def test_compute_terse_response_shape(http_app):
    response = http_app.client.post("/v1/compute", json=_compute_body())
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"]["symbol"] == "NIFTY"
    assert body["timeframe"] == "1W"
    assert "latest_change" in body
    assert "latest_price" in body
    assert "bars" not in body


def test_compute_verbose_response_shape(http_app):
    response = http_app.client.post(
        "/v1/compute", json=_compute_body(verbose=True),
    )
    assert response.status_code == 200
    body = response.json()
    assert "bars" in body
    assert len(body["bars"]) > 0
    assert "latest_change" in body


def test_compute_empty_history_only_latest_bar(http_app):
    body = _compute_body(n_history=0)
    response = http_app.client.post("/v1/compute", json=body)
    assert response.status_code == 200
    # With no history to compare against, latest_change is NO_CHANGE.
    assert response.json()["latest_change"] == "NO_CHANGE"


def test_compute_rejects_naive_timestamp(http_app):
    """OHLCVBar requires tz-aware timestamps; naive should 422."""
    body = _compute_body()
    body["latest_bar"]["timestamp"] = "2024-01-01T00:00:00"  # no tz
    response = http_app.client.post("/v1/compute", json=body)
    assert response.status_code == 422


def test_compute_rejects_missing_required_field(http_app):
    body = _compute_body()
    del body["latest_bar"]
    response = http_app.client.post("/v1/compute", json=body)
    assert response.status_code == 422


def test_compute_rejects_extra_field(http_app):
    """Pydantic forbid-extra catches typos in request bodies."""
    body = _compute_body()
    body["unknown_field"] = "x"
    response = http_app.client.post("/v1/compute", json=body)
    assert response.status_code == 422


# PHASE-3E: test_compute_does_not_touch_cache deleted. The legacy
# SignalCache it inspected no longer exists; /v1/compute is
# structurally stateless (has no cache to touch).


def test_compute_uses_default_timeframe(http_app):
    body = _compute_body()
    del body["timeframe"]   # let it default
    response = http_app.client.post("/v1/compute", json=body)
    assert response.status_code == 200
    assert response.json()["timeframe"] == "1W"
