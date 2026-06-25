"""Tests for /v1/pulse and /v1/pulse/stream.

Strategy:
  * Snapshot endpoint (GET /v1/pulse): tested with FastAPI's TestClient.
    Synchronous, fast, exercises the serialization path.
  * SSE endpoint (GET /v1/pulse/stream): tested with httpx.AsyncClient
    against an ASGI transport. TestClient doesn't handle SSE well
    because the response body never completes.

We build a minimal FastAPI app per test (or per fixture) that includes
only the pulse router. The real `create_app()` is exercised in Step 6
integration tests, not here.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone, UTC

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trip_ema.scheduler.market_orchestrator import (
    OrchestratorRunSummary,
)
from trip_ema.transports.http.dependencies import (
    get_pulse_state,
)
from trip_ema.transports.http.pulse_state import PulseState
from trip_ema.transports.http.routes import pulse as pulse_routes


UTC = UTC


def _build_app(pulse_state):
    """Minimal FastAPI app with just the pulse router."""
    app = FastAPI()
    app.include_router(pulse_routes.router)
    app.dependency_overrides[get_pulse_state] = lambda: pulse_state
    return app


def _dummy_summary(exchange: str = "NSE", succeeded: bool = True) -> OrchestratorRunSummary:
    s = OrchestratorRunSummary(
        exchange=exchange,
        started_at=datetime(2026, 5, 19, 5, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 19, 5, 0, 1, tzinfo=UTC),
        correlation_id=f"cid-{exchange}",
    )
    s.fetch.succeeded = succeeded
    s.fetch.duration_seconds = 0.5
    s.compute.succeeded = succeeded
    s.compute.duration_seconds = 1.2
    s.distribute.succeeded = succeeded
    s.distribute.duration_seconds = 0.3
    return s


# ---------------------------------------------------------------------------
# GET /v1/pulse (snapshot)
# ---------------------------------------------------------------------------


class TestPulseSnapshot:
    def test_returns_service_started_event_when_empty(self):
        ps = PulseState()
        app = _build_app(ps)
        with TestClient(app) as client:
            resp = client.get("/v1/pulse")
        assert resp.status_code == 200
        body = resp.json()
        assert "service_started_at" in body
        assert body["subscriber_count"] == 0
        assert body["published_events_total"] == 0
        assert body["dropped_events_total"] == 0
        # Only the seeded service_started event so far.
        assert len(body["events"]) == 1
        assert body["events"][0]["event_type"] == "service_started"

    def test_event_payload_includes_step_results(self):
        ps = PulseState()
        ps.publish(_dummy_summary("NSE"))
        app = _build_app(ps)
        with TestClient(app) as client:
            resp = client.get("/v1/pulse")
        events = resp.json()["events"]
        # The seeded service_started, then the orchestrator_run.
        assert len(events) == 2
        run_event = events[-1]
        assert run_event["event_type"] == "orchestrator_run"
        assert run_event["exchange"] == "NSE"
        assert run_event["correlation_id"] == "cid-NSE"
        assert run_event["fetch"]["succeeded"] is True
        assert run_event["fetch"]["duration_seconds"] == 0.5
        assert run_event["compute"]["succeeded"] is True
        assert run_event["distribute"]["succeeded"] is True
        assert run_event["is_retry"] is False
        assert run_event["retry_scheduled"] is False

    def test_failed_step_includes_error_message(self):
        ps = PulseState()
        s = _dummy_summary("NSE", succeeded=False)
        s.fetch.error_message = "yfinance is down"
        ps.publish(s)
        app = _build_app(ps)
        with TestClient(app) as client:
            resp = client.get("/v1/pulse")
        run_event = resp.json()["events"][-1]
        assert run_event["fetch"]["succeeded"] is False
        assert run_event["fetch"]["error_message"] == "yfinance is down"

    def test_service_level_events_have_null_step_fields(self):
        ps = PulseState()
        ps.publish_orchestrator_started("NSE")
        ps.publish_orchestrator_stopped("BSE", reason="crash")
        app = _build_app(ps)
        with TestClient(app) as client:
            resp = client.get("/v1/pulse")
        events = resp.json()["events"]
        started = next(e for e in events if e["event_type"] == "orchestrator_started")
        stopped = next(e for e in events if e["event_type"] == "orchestrator_stopped")
        assert started["exchange"] == "NSE"
        assert started["fetch"] is None
        assert started["compute"] is None
        assert started["distribute"] is None
        assert stopped["exchange"] == "BSE"
        assert stopped["details"] == {"reason": "crash"}

    def test_503_when_pulse_state_unset(self):
        """Orchestrator disabled → pulse endpoint reports 503."""
        app = FastAPI()
        app.include_router(pulse_routes.router)
        app.dependency_overrides[get_pulse_state] = lambda: None
        with TestClient(app) as client:
            resp = client.get("/v1/pulse")
        assert resp.status_code == 503
        assert "orchestrator is not running" in resp.json()["detail"]

    def test_counters_reflect_actual_publish_count(self):
        ps = PulseState()
        for _ in range(3):
            ps.publish(_dummy_summary("NSE"))
        app = _build_app(ps)
        with TestClient(app) as client:
            resp = client.get("/v1/pulse")
        body = resp.json()
        assert body["published_events_total"] == 3
        assert body["dropped_events_total"] == 0


# ---------------------------------------------------------------------------
# GET /v1/pulse/stream (SSE)
# ---------------------------------------------------------------------------


def _parse_sse_frames(text: str) -> list[dict]:
    """Parse SSE text into a list of {event, data_json} dicts.

    Comment lines (starting with ':') are skipped. We expect each event
    to look like:
        event: orchestrator_run
        data: {...json...}
    separated by blank lines.
    """
    frames = []
    for block in text.split("\n\n"):
        event_type = None
        data_lines = []
        for line in block.splitlines():
            if line.startswith(":"):
                continue  # heartbeat/comment
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            frames.append({
                "event": event_type,
                "data": json.loads("\n".join(data_lines)),
            })
    return frames


class TestPulseStream:
    @pytest.fixture(autouse=True)
    def _fast_heartbeat(self, monkeypatch):
        """Tests don't want to wait 20s for a heartbeat. We patch the
        constant to 0.1s for the duration of the SSE test class."""
        monkeypatch.setattr(pulse_routes, "_HEARTBEAT_INTERVAL_SECONDS", 0.1)

    @pytest.mark.asyncio
    async def test_stream_emits_backfill_then_live_events(self):
        ps = PulseState()
        ps.bind_loop(asyncio.get_running_loop())
        # Seed one historical event so we can verify backfill works.
        ps.publish(_dummy_summary("HIST"))

        app = _build_app(ps)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
        ) as client:
            # Publish the LIVE event BEFORE we connect, so it's already
            # in the ring buffer. The backfill mechanism will pick it up
            # as part of the snapshot. This removes the publish-while-
            # streaming timing fragility — we still verify the same
            # behaviour (live events arrive at subscribers) via the
            # separate `test_subscriber_receives_live_events` test that
            # uses the PulseState directly without HTTP framing.
            ps.publish(_dummy_summary("LIVE"))

            async with client.stream(
                "GET", "/v1/pulse/stream", timeout=5.0,
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith(
                    "text/event-stream",
                )

                received = []
                async for chunk in resp.aiter_text():
                    received.append(chunk)
                    frames = _parse_sse_frames("".join(received))
                    # service_started + HIST + LIVE
                    if len(frames) >= 3:
                        break

        frames = _parse_sse_frames("".join(received))
        types = [f["event"] for f in frames]
        assert types[:3] == [
            "service_started", "orchestrator_run", "orchestrator_run",
        ]
        # Confirm the two backfilled orchestrator_runs preserve order
        # (HIST seeded first, then LIVE).
        assert frames[1]["data"]["exchange"] == "HIST"
        assert frames[2]["data"]["exchange"] == "LIVE"

    @pytest.mark.asyncio
    async def test_stream_delivers_live_event_published_during_stream(self):
        """Verify that an event published AFTER the stream is open is
        delivered. The test drains the stream until both events are
        seen (or timeout) rather than chunk-counting — httpx's ASGI
        transport bundles small chunks together, so chunk == frame is
        not a safe assumption."""
        ps = PulseState()
        ps.bind_loop(asyncio.get_running_loop())
        app = _build_app(ps)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
        ) as client:
            async def publish_after_delay():
                # Give the stream a moment to connect and emit the
                # `: connected` prelude. Then publish a LIVE event.
                await asyncio.sleep(0.05)
                ps.publish(_dummy_summary("LIVE"))

            publish_task = asyncio.create_task(publish_after_delay())

            async with client.stream(
                "GET", "/v1/pulse/stream", timeout=3.0,
            ) as resp:
                received = []
                async for chunk in resp.aiter_text():
                    received.append(chunk)
                    frames = _parse_sse_frames("".join(received))
                    types = [f["event"] for f in frames]
                    # Loop until we've seen the service_started backfill
                    # AND the LIVE event.
                    if "service_started" in types and any(
                        f.get("data", {}).get("exchange") == "LIVE"
                        for f in frames
                    ):
                        break

            await publish_task

        frames = _parse_sse_frames("".join(received))
        live = [
            f for f in frames
            if f.get("data", {}).get("exchange") == "LIVE"
        ]
        assert len(live) == 1, (
            f"Expected one LIVE frame, got frames: "
            f"{[f['event'] + ':' + str(f['data'].get('exchange')) for f in frames]}"
        )

    @pytest.mark.asyncio
    async def test_stream_503_when_pulse_state_unset(self):
        app = FastAPI()
        app.include_router(pulse_routes.router)
        app.dependency_overrides[get_pulse_state] = lambda: None

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
        ) as client:
            resp = await client.get("/v1/pulse/stream")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_stream_sets_no_buffer_headers(self):
        ps = PulseState()
        ps.bind_loop(asyncio.get_running_loop())
        app = _build_app(ps)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
        ) as client, client.stream(
            "GET", "/v1/pulse/stream", timeout=2.0,
        ) as resp:
            assert resp.headers.get("x-accel-buffering") == "no"
            assert resp.headers.get("cache-control") == "no-cache"
            # The `: connected` comment is emitted immediately, so
            # we don't need to wait for a real event to confirm
            # streaming. Just drain one chunk and exit.
            async for chunk in resp.aiter_text():
                assert ":" in chunk  # comment line marker
                break
