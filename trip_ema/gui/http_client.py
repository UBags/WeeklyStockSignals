"""HTTP client for the trip_ema admin endpoints.

Thin wrapper around `requests` with:
  - Bearer-token auth read from an env var on startup.
  - One method per admin endpoint we hit from the GUI.
  - Returns parsed JSON dicts (not Pydantic models — the GUI side
    treats them as plain data).
  - Raises ApiError on non-2xx with a message extracted from the
    JSON `detail` field where available.

Designed so the GUI's BackgroundWorker can just call
`client.run_eod()` from a worker thread and emit the result over a
signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """Wraps a non-2xx response with the message from `detail` if
    present, else the raw body."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


@dataclass(frozen=True, slots=True)
class HttpClientConfig:
    base_url: str
    """Where the trip_ema main service is listening. e.g.
    'http://localhost:8000'."""
    token: Optional[str]
    """Admin bearer token. None disables action buttons in the UI."""
    timeout_seconds: float = 30.0
    """Request timeout. Status reads should finish in <1s; action
    submits in <2s (they're fire-and-forget). Generous timeout
    accommodates a slow Postgres on the server side."""


class TripEmaHttpClient:

    def __init__(self, config: HttpClientConfig):
        self._config = config
        self._session = requests.Session()
        if config.token:
            self._session.headers.update({
                "Authorization": f"Bearer {config.token}",
            })

    @property
    def token_configured(self) -> bool:
        """The GUI uses this to enable/disable action buttons."""
        return bool(self._config.token)

    # ---------- ETL actions (POST) ---------- #

    def refresh_listings(self, *, exchange: str = "NSE") -> dict:
        return self._post(
            "/v1/admin/etl/refresh-listings",
            params={"exchange": exchange},
        )

    def run_eod(self, *, exchange: str = "NSE", lookback_days: int = 7) -> dict:
        return self._post(
            "/v1/admin/etl/run-eod",
            params={"exchange": exchange, "lookback_days": lookback_days},
        )

    def run_intraday(self, *, exchange: str = "NSE") -> dict:
        return self._post(
            "/v1/admin/etl/run-intraday",
            params={"exchange": exchange},
        )

    def recompute_tiers(self, *, exchange: str = "NSE") -> dict:
        return self._post(
            "/v1/admin/etl/recompute-tiers",
            params={"exchange": exchange},
        )

    def unpark_all(self, *, exchange: str = "NSE") -> dict:
        return self._post(
            "/v1/admin/etl/unpark-all",
            params={"exchange": exchange},
        )

    def backfill(
        self, *, start_date: date, tier: Optional[int] = None,
        exchange: str = "NSE",
    ) -> dict:
        params: dict = {
            "exchange": exchange,
            "start_date": start_date.isoformat(),
        }
        if tier is not None:
            params["tier"] = tier
        return self._post("/v1/admin/etl/backfill", params=params)

    # ---------- ETL status reads (GET) ---------- #

    def get_etl_status(
        self, *, limit: int = 20, exchange: Optional[str] = None,
    ) -> dict:
        """Phase 14.6c: optional `exchange` scopes the tier_counts
        to one exchange. When omitted, server falls back to
        config.etl.exchange for backward compatibility."""
        params: dict = {"limit": limit}
        if exchange is not None:
            params["exchange"] = exchange
        return self._get("/v1/admin/etl/status", params=params)

    def get_etl_parked(self) -> dict:
        return self._get("/v1/admin/etl/parked")

    # ---------- Exchange registry + state (Phase 14.6c) ---------- #

    def list_exchanges(self) -> dict:
        """Return the configured-exchanges payload from the server.

        Shape:
          {
            "exchanges": ["BSE", "NSE", "NYSE"],
            "total": 3,
            "snapshot_taken_at": "...",
          }
        Used by the GUI's exchange picker to populate its dropdown.
        """
        return self._get("/v1/admin/exchanges")

    def get_exchange_state(self, exchange: str) -> dict:
        """Return the exchange_state row for one exchange.

        Returns the ExchangeStatePayload shape. Raises ApiError(404)
        if no row exists for that exchange (i.e. no ETL activity yet).
        """
        return self._get(f"/v1/admin/exchange-state/{exchange}")

    def list_exchange_states(self) -> dict:
        """Return every exchange_state row. ExchangeStateListPayload shape."""
        return self._get("/v1/admin/exchange-state")

    # ---------- Signal data (GET) — for the chart view ---------- #

    def get_signal_history(
        self, exchange: str, symbol: str,
        *, instrument: str = "EQ", years: int = 3,
    ) -> dict:
        """Fetch the full bar-by-bar indicator history for the chart.

        Returns the SignalHistoryPayload shape:
          { bars: [...], signal_events: [...], warmup_bars_count, ... }
        """
        return self._get(
            f"/v1/signals/{exchange}/{symbol}/history",
            params={"instrument": instrument, "years": years},
        )

    # ---------- Subscribers admin (Slice 4) ---------- #

    def list_subscribers(self) -> dict:
        return self._get("/v1/admin/subscribers")

    def get_subscriber(self, subscriber_id: int) -> dict:
        return self._get(f"/v1/admin/subscribers/{subscriber_id}")

    def create_subscriber(
        self, *, transport: str, address: str,
        notify_cadence_minutes: int, is_active: bool = True,
        filters: Optional[list] = None,
    ) -> dict:
        body = {
            "transport": transport, "address": address,
            "notify_cadence_minutes": notify_cadence_minutes,
            "is_active": is_active,
        }
        if filters is not None:
            body["filters"] = filters
        return self._post_json("/v1/admin/subscribers", body=body)

    def update_subscriber(
        self, subscriber_id: int,
        *, transport: Optional[str] = None,
        address: Optional[str] = None,
        notify_cadence_minutes: Optional[int] = None,
        is_active: Optional[bool] = None,
    ) -> dict:
        body: dict = {}
        if transport is not None:
            body["transport"] = transport
        if address is not None:
            body["address"] = address
        if notify_cadence_minutes is not None:
            body["notify_cadence_minutes"] = notify_cadence_minutes
        if is_active is not None:
            body["is_active"] = is_active
        return self._request_json(
            "PATCH", f"/v1/admin/subscribers/{subscriber_id}", body=body,
        )

    def delete_subscriber(self, subscriber_id: int) -> None:
        """DELETE returns 204 No Content; we return None."""
        self._request_json(
            "DELETE", f"/v1/admin/subscribers/{subscriber_id}",
            expect_no_content=True,
        )

    def replace_filters(
        self, subscriber_id: int, filters: list,
    ) -> dict:
        """PUT the canonical filter list — replaces the DB rows wholesale."""
        return self._request_json(
            "PUT", f"/v1/admin/subscribers/{subscriber_id}/filters",
            body=filters,
        )

    def test_notification(self, subscriber_id: int) -> dict:
        return self._post(
            f"/v1/admin/subscribers/{subscriber_id}/test-notification",
        )

    # ---------- Notifications (Slice 4) ---------- #

    def list_recent_notifications(
        self, *, limit: int = 100, since_hours: int = 24 * 7,
        subscriber_id: Optional[int] = None,
    ) -> dict:
        params: dict = {"limit": limit, "since_hours": since_hours}
        if subscriber_id is not None:
            params["subscriber_id"] = subscriber_id
        return self._get("/v1/admin/notifications/recent", params=params)

    # ---------- Health snapshot (Slice 4) ---------- #

    def get_health_snapshot(self) -> dict:
        return self._get("/v1/admin/health-snapshot")

    # ---------- Ticker actions (POST) ---------- #

    def reprime_ticker(self, exchange: str, symbol: str) -> dict:
        return self._post(
            f"/v1/admin/reprime-ticker/{exchange}/{symbol}",
        )

    # ---------- internal ---------- #

    def _get(self, path: str, *, params: Optional[dict] = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, *, params: Optional[dict] = None) -> dict:
        return self._request("POST", path, params=params)

    def _post_json(self, path: str, *, body) -> dict:
        return self._request_json("POST", path, body=body)

    def _request_json(
        self, method: str, path: str,
        *, body=None, expect_no_content: bool = False,
    ) -> dict:
        """Variant of _request that sends `body` as JSON. Pass
        expect_no_content=True for DELETE (returns {})."""
        url = f"{self._config.base_url}{path}"
        try:
            resp = self._session.request(
                method, url, json=body,
                timeout=self._config.timeout_seconds,
            )
        except requests.RequestException as e:
            raise ApiError(0, f"network error: {e}") from e

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("detail", "") or ""
            except Exception:
                detail = resp.text[:200]
            raise ApiError(resp.status_code, detail or f"HTTP {resp.status_code}")
        if expect_no_content or resp.status_code == 204:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(resp.status_code, f"invalid JSON: {e}") from e

    def _request(
        self, method: str, path: str,
        *, params: Optional[dict] = None,
    ) -> dict:
        url = f"{self._config.base_url}{path}"
        try:
            resp = self._session.request(
                method, url, params=params,
                timeout=self._config.timeout_seconds,
            )
        except requests.RequestException as e:
            # Network failure — surface as 0 status with the original
            # error message. The GUI can't distinguish "main service
            # is down" from "wrong host" from "firewall" without more
            # plumbing, and they all need the same user response
            # ("check the service is up").
            raise ApiError(0, f"network error: {e}") from e

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("detail", "") or ""
            except Exception:
                detail = resp.text[:200]
            raise ApiError(resp.status_code, detail or f"HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(resp.status_code, f"invalid JSON: {e}") from e
