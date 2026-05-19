"""HTTP payloads for Phase 14 endpoints (orchestrator admin, pulse).

Kept in a separate module from `schemas.py` to make the Phase 14
diff small and reviewable. The two modules can be merged later if
that fits the project's style — there's no functional reason to
keep them separate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Orchestrator status payloads (GET /v1/admin/orchestrator/status)
# ---------------------------------------------------------------------------


class OrchestratorStepStatusPayload(BaseModel):
    """One step (fetch | compute | distribute) within the last run.

    Mirrors `OrchestratorStepResult` from the scheduler. We strip the
    opaque `detail` field — admin/GUI consumers don't need the raw
    ComputeRunSummary or DistributionSummary blobs."""
    model_config = ConfigDict(extra="forbid")

    succeeded: bool
    skipped_reason: Optional[str] = None
    error_message: Optional[str] = None
    duration_seconds: float


class OrchestratorMarketStatusPayload(BaseModel):
    """Status for one market's orchestrator."""
    model_config = ConfigDict(extra="forbid")

    exchange: str
    is_alive: bool
    is_unhealthy: bool
    last_run_started_at: Optional[datetime] = None
    last_run_finished_at: Optional[datetime] = None
    last_run_correlation_id: Optional[str] = None
    last_run_was_retry: bool = False
    last_run_retry_scheduled: bool = False
    fetch: Optional[OrchestratorStepStatusPayload] = None
    compute: Optional[OrchestratorStepStatusPayload] = None
    distribute: Optional[OrchestratorStepStatusPayload] = None


class OrchestratorStatusPayload(BaseModel):
    """Top-level status: every managed market plus pool-level health."""
    model_config = ConfigDict(extra="forbid")

    pool_size: int
    any_unhealthy: bool = Field(
        description="True if at least one orchestrator was started but its "
                    "thread has died. Operator should restart the service.",
    )
    markets: list[OrchestratorMarketStatusPayload]


# ---------------------------------------------------------------------------
# Run-now action acknowledgement (POST endpoints)
# ---------------------------------------------------------------------------


class OrchestratorRunNowAcceptedPayload(BaseModel):
    """Returned by run-now and run-now-all. The actual pipeline runs
    asynchronously on a background executor; the response indicates
    only that the request was accepted and queued."""
    model_config = ConfigDict(extra="forbid")

    action: str = Field(description="'run-now' | 'run-now-all'")
    exchanges: list[str] = Field(
        description="Exchanges the pipeline will fire for. One entry for "
                    "run-now; all configured exchanges for run-now-all.",
    )
    accepted_at: datetime


# ---------------------------------------------------------------------------
# Pulse snapshot (GET /v1/pulse)
# ---------------------------------------------------------------------------


class PulseEventPayload(BaseModel):
    """One pulse event, serialized for the snapshot endpoint.

    The `summary` field of `PulseEvent` (an `OrchestratorRunSummary`)
    is flattened here into the same shape as
    `OrchestratorMarketStatusPayload.last_run_*` for client uniformity.
    Service-level events (`service_started`, `orchestrator_started`,
    `orchestrator_stopped`) leave the summary fields null."""
    model_config = ConfigDict(extra="forbid")

    event_type: str
    timestamp: datetime
    exchange: Optional[str] = None
    correlation_id: Optional[str] = None
    is_retry: bool = False
    retry_scheduled: bool = False
    fetch: Optional[OrchestratorStepStatusPayload] = None
    compute: Optional[OrchestratorStepStatusPayload] = None
    distribute: Optional[OrchestratorStepStatusPayload] = None
    details: Optional[dict] = None


class PulseSnapshotPayload(BaseModel):
    """The /v1/pulse response."""
    model_config = ConfigDict(extra="forbid")

    service_started_at: datetime = Field(
        description="When the PulseState was constructed — i.e., when the "
                    "service started. GUI clients can detect restarts by "
                    "watching this value.",
    )
    subscriber_count: int = Field(
        description="Live SSE subscribers connected to /v1/pulse/stream."
    )
    published_events_total: int
    dropped_events_total: int = Field(
        description="Events dropped because at least one subscriber's "
                    "queue was full. A non-zero value suggests a slow "
                    "subscriber falling behind.",
    )
    events: list[PulseEventPayload] = Field(
        description="Ring buffer of recent events, most-recent last.",
    )
