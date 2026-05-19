"""Pulse endpoints — read-only, no admin auth.

GET /v1/pulse          — snapshot of recent orchestrator events
GET /v1/pulse/stream   — Server-Sent Events stream of new events

Why no auth: pulse is meant for the GUI to know "did anything just
happen". It carries no secrets — only orchestrator pipeline statuses
the operator already sees via admin endpoints. If the deployment
posture changes (e.g., putting the service behind a public reverse
proxy), the operator can add a layer of basic auth at the proxy
level. Keeping the endpoint open by default mirrors `/healthz` and
`/readyz`.

The SSE implementation is hand-rolled — no `sse-starlette` dependency.
Format: `data: <json>\\n\\n` per event. Standard browser EventSource
and Python `httpx` clients parse this without further framing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from ..dependencies import get_pulse_state
from ..orchestrator_schemas import (
    OrchestratorStepStatusPayload,
    PulseEventPayload,
    PulseSnapshotPayload,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/v1", tags=["pulse"])


# ---------------------------------------------------------------------------
# Translation: PulseEvent → PulseEventPayload
# ---------------------------------------------------------------------------


def _step_to_payload(step) -> Optional[OrchestratorStepStatusPayload]:
    """Build a step payload from an OrchestratorStepResult, or None
    if the step never ran (concurrent-skipped case)."""
    if step is None:
        return None
    return OrchestratorStepStatusPayload(
        succeeded=step.succeeded,
        skipped_reason=step.skipped_reason,
        error_message=step.error_message,
        duration_seconds=step.duration_seconds,
    )


def _event_to_payload(event) -> PulseEventPayload:
    """Translate the internal PulseEvent into the wire shape.

    `summary` (an OrchestratorRunSummary on orchestrator_run events)
    is flattened so clients get a uniform view across event types.
    Service-level events have summary=None and just expose `details`."""
    summary = event.summary
    if summary is None:
        return PulseEventPayload(
            event_type=event.event_type,
            timestamp=event.timestamp,
            exchange=event.exchange,
            details=event.details,
        )
    return PulseEventPayload(
        event_type=event.event_type,
        timestamp=event.timestamp,
        exchange=summary.exchange,
        correlation_id=summary.correlation_id,
        is_retry=summary.is_retry,
        retry_scheduled=summary.retry_scheduled,
        fetch=_step_to_payload(summary.fetch),
        compute=_step_to_payload(summary.compute),
        distribute=_step_to_payload(summary.distribute),
        details=event.details,
    )


def _require_pulse(pulse_state):
    """503 if pulse is unavailable (orchestrator disabled)."""
    if pulse_state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Pulse is unavailable: the orchestrator is not running. "
                "Set `scheduling.orchestrator.enabled=true` in config to "
                "enable per-market orchestration."
            ),
        )


# ---------------------------------------------------------------------------
# GET /v1/pulse — snapshot endpoint
# ---------------------------------------------------------------------------


@router.get("/pulse", response_model=PulseSnapshotPayload)
def pulse_snapshot(
    pulse_state = Depends(get_pulse_state),
) -> PulseSnapshotPayload:
    """Return the current ring buffer plus service-level counters.

    Intended for GUI polling. The GUI typically polls this every
    1-5 seconds and reacts to changes in `service_started_at` (service
    restarted) or the most-recent event's correlation_id (new pipeline
    pass). For lower-latency updates, subscribe to /v1/pulse/stream.
    """
    _require_pulse(pulse_state)
    events = [_event_to_payload(e) for e in pulse_state.snapshot()]
    return PulseSnapshotPayload(
        service_started_at=pulse_state.service_started_at,
        subscriber_count=pulse_state.subscriber_count,
        published_events_total=pulse_state.published_events_total,
        dropped_events_total=pulse_state.dropped_events_total,
        events=events,
    )


# ---------------------------------------------------------------------------
# GET /v1/pulse/stream — Server-Sent Events
# ---------------------------------------------------------------------------


# Heartbeat cadence: send a comment line every N seconds so idle
# connections aren't silently torn down by intermediate proxies.
# Keep small enough that NGINX's default 60s read timeout doesn't fire,
# but large enough not to burn bandwidth.
_HEARTBEAT_INTERVAL_SECONDS = 20.0


async def _event_stream(
    request: Request,
    subscription,
) -> AsyncIterator[bytes]:
    """Generator yielding text/event-stream frames.

    The subscription is constructed by the route handler (NOT here)
    so that the subscriber queue is registered with PulseState
    BEFORE any client-side bytes are produced. This matters because
    async generators don't run their body until first iterated —
    if we called `open_subscription()` here, registration would be
    deferred until httpx pulled the first chunk, opening a race where
    events published during that window land only in the ring buffer.

    Closes cleanly when:
      * The client disconnects (detected via `request.is_disconnected()`).
      * The PulseState subscription's live-events iterator exits.

    Each event is one JSON object with the same shape as
    PulseEventPayload. Plus periodic heartbeats (comment lines) to
    keep proxies happy on idle connections.
    """
    try:
        # Initial heartbeat so the client knows the stream is alive
        # before any real event arrives.
        yield b": connected\n\n"

        # Send the backfill snapshot (events from before subscription).
        for event in subscription.backfill:
            payload = _event_to_payload(event)
            data = payload.model_dump_json()
            yield f"event: {event.event_type}\ndata: {data}\n\n".encode("utf-8")

        # Now enter the live-events loop. We interleave with periodic
        # heartbeats via asyncio.wait + timeout.
        live_iter = subscription.events().__aiter__()
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected; closing stream")
                break

            try:
                next_event_task = asyncio.create_task(live_iter.__anext__())
                done, pending = await asyncio.wait(
                    {next_event_task},
                    timeout=_HEARTBEAT_INTERVAL_SECONDS,
                )
                if not done:
                    # Timed out → heartbeat.
                    next_event_task.cancel()
                    try:
                        await next_event_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    yield b": heartbeat\n\n"
                    continue

                event = next_event_task.result()
            except StopAsyncIteration:
                logger.info("SSE subscriber iterator exhausted; closing stream")
                break

            payload = _event_to_payload(event)
            data = payload.model_dump_json()
            yield f"event: {event.event_type}\ndata: {data}\n\n".encode("utf-8")
    finally:
        # Unregister the subscriber so the queue is removed from
        # PulseState's subscriber set. Best-effort cleanup.
        try:
            subscription.close()
        except Exception:
            logger.exception("Error closing pulse subscription")


@router.get("/pulse/stream")
async def pulse_stream(
    request: Request,
    pulse_state = Depends(get_pulse_state),
):
    """Server-Sent Events stream of pulse events.

    Connection lifecycle:
      * Server sends `: connected` immediately, then the backfill of
        recent events from the ring buffer (one event per frame).
      * Live events are pushed as the orchestrator publishes them.
      * Every ~20s the server sends a `: heartbeat` comment to keep
        intermediate proxies from idle-closing the connection.
      * Either side can close at any time. We honor disconnect within
        one heartbeat tick (`_HEARTBEAT_INTERVAL_SECONDS`).
    """
    _require_pulse(pulse_state)
    # Open the subscription synchronously here, BEFORE returning the
    # StreamingResponse. The async generator inside StreamingResponse
    # won't run its body until httpx pulls the first chunk; if we
    # opened the subscription there, registration would be deferred
    # past the request handler's return. By opening here, the
    # subscriber queue is registered the moment FastAPI invokes this
    # handler — closing the race where a publish() between handler
    # invocation and first __anext__() would land only in the ring
    # buffer.
    subscription = pulse_state.open_subscription()
    return StreamingResponse(
        _event_stream(request, subscription),
        media_type="text/event-stream",
        headers={
            # Disable proxy-level buffering. Without this, NGINX (and some
            # CDNs) buffer the response and the client sees nothing until
            # the body completes — which for SSE is never.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            # SSE keepalive — the heartbeat handles this server-side, but
            # tell the client+intermediaries that we want the connection
            # held open.
            "Connection": "keep-alive",
        },
    )
