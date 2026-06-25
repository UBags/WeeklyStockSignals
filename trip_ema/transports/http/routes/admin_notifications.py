"""Notification log admin endpoint (Slice 4).

  GET    /v1/admin/notifications/recent?limit=&since=&subscriber_id=
        — list recent notification_log entries with joined subscriber
          + event + ticker info for human-readable display.

Phase 12 — DLQ management:

  GET    /v1/admin/notifications/dlq?limit=
        — list jobs currently in the notifier DLQ
  POST   /v1/admin/notifications/dlq/{job_id}/replay
        — move a DLQ entry back to the main queue for another attempt
  DELETE /v1/admin/notifications/dlq/{job_id}
        — permanently delete a DLQ entry

The notification_log table is partitioned by `last_sent_at` (monthly).
Postgres handles partition pruning automatically when WHERE includes
last_sent_at filters, so the `since` parameter dramatically speeds up
reads.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Engine, text

from ..dependencies import get_db_engine, get_redis_queue, require_admin_token
from ..schemas import (
    DlqActionResult, DlqEntry, DlqListPayload,
    NotificationLogEntry, NotificationLogPayload,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/admin/notifications",
    tags=["admin", "notifications"],
    dependencies=[Depends(require_admin_token)],
)


@router.get("/recent", response_model=NotificationLogPayload)
def list_recent_notifications(
    limit: int = Query(100, ge=1, le=1000),
    since_hours: int = Query(
        24 * 7, ge=1, le=24 * 90,
        description="How far back to look, in hours. Default 168 (1 week).",
    ),
    subscriber_id: int | None = Query(None),
    db_engine: Engine = Depends(get_db_engine),
) -> NotificationLogPayload:
    """Recent notification_log rows joined with subscribers + events
    + tickers. Most-recent first.

    has_more=True when the query hit the limit; the GUI can offer a
    'show older' button that bumps `since_hours`."""
    since = datetime.now(UTC) - timedelta(hours=since_hours)
    params: dict = {"since": since, "lim": limit}
    sub_filter_sql = ""
    if subscriber_id is not None:
        sub_filter_sql = " AND nl.subscriber_id = :sid"
        params["sid"] = subscriber_id

    sql = f"""
        SELECT nl.log_id, nl.last_sent_at, nl.subscriber_id,
               nl.event_id, nl.notification_count,
               s.address AS sub_address, s.transport AS sub_transport,
               e.signal_type AS event_signal, e.bar_timestamp AS event_bar_timestamp,
               t.symbol AS ticker_symbol, t.exchange AS ticker_exchange
        FROM notification_log nl
        JOIN subscribers s ON nl.subscriber_id = s.subscriber_id
        JOIN signal_events e ON nl.event_id = e.event_id
        LEFT JOIN tickers t ON e.ticker_id = t.ticker_id
        WHERE nl.last_sent_at >= :since {sub_filter_sql}
        ORDER BY nl.last_sent_at DESC
        LIMIT :lim
    """

    with db_engine.connect() as conn:
        rows = list(conn.execute(text(sql), params))

    entries = [
        NotificationLogEntry(
            log_id=r.log_id,
            last_sent_at=r.last_sent_at,
            subscriber_id=r.subscriber_id,
            subscriber_address=r.sub_address,
            subscriber_transport=r.sub_transport,
            event_id=r.event_id,
            event_signal=str(r.event_signal),
            event_bar_timestamp=r.event_bar_timestamp,
            ticker_symbol=r.ticker_symbol,
            ticker_exchange=r.ticker_exchange,
            notification_count=r.notification_count,
        )
        for r in rows
    ]
    return NotificationLogPayload(
        entries=entries,
        total=len(entries),
        has_more=(len(entries) == limit),
        snapshot_taken_at=datetime.now(UTC),
    )


# ============================================================
# DLQ endpoints (Phase 12)
# ============================================================


def _dlq_entry_from_payload(raw: dict) -> DlqEntry:
    """Translate a raw DLQ JSON payload into the typed schema."""
    failed_at_epoch = raw.get("_dlq_at")
    failed_at = (
        datetime.fromtimestamp(failed_at_epoch, tz=UTC)
        if failed_at_epoch is not None
        else datetime.now(UTC)
    )
    return DlqEntry(
        job_id=raw.get("_job_id", "?"),
        transport=raw.get("transport", "unknown"),
        subscriber_id=raw.get("subscriber_id"),
        event_id=raw.get("event_id"),
        attempts=int(raw.get("_attempts", 0)),
        failed_at=failed_at,
        reason=raw.get("_dlq_reason", "unknown"),
        raw_payload=raw,
    )


@router.get("/dlq", response_model=DlqListPayload)
def list_dlq(
    limit: int = Query(100, ge=1, le=1000),
    redis_queue=Depends(get_redis_queue),
) -> DlqListPayload:
    """List jobs currently in the notifier DLQ.

    Most-recent first (Redis LPUSH on insertion, LRANGE reads from
    head). The list is bounded by `limit`; the total count includes
    entries beyond the limit so operators know if the DLQ is deep.
    """
    if redis_queue is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Notifier queue not configured (no Redis).",
        )
    raw_entries = redis_queue.list_dlq(limit=limit)
    entries = [_dlq_entry_from_payload(r) for r in raw_entries]
    total = redis_queue.dlq_depth()
    return DlqListPayload(
        entries=entries,
        total=total,
        snapshot_taken_at=datetime.now(UTC),
    )


@router.post(
    "/dlq/{job_id}/replay",
    response_model=DlqActionResult,
)
def replay_dlq_entry(
    job_id: str,
    redis_queue=Depends(get_redis_queue),
) -> DlqActionResult:
    """Move a DLQ entry back to the main queue. Resets the attempts
    counter so the job starts a fresh retry cycle.

    Use after the underlying transport problem has been resolved
    (e.g., SMTP server back up, webhook URL fixed). If the problem
    persists, the job will hit max_attempts again and return to the
    DLQ — replaying repeatedly without fixing the cause is futile.
    """
    if redis_queue is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Notifier queue not configured (no Redis).",
        )
    ok = redis_queue.replay_from_dlq(job_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No DLQ entry with job_id={job_id!r}",
        )
    logger.info("DLQ entry replayed", extra={"job_id": job_id})
    return DlqActionResult(
        job_id=job_id,
        action="replay",
        success=True,
        message="Job pushed back onto the main queue.",
    )


@router.delete(
    "/dlq/{job_id}",
    response_model=DlqActionResult,
)
def delete_dlq_entry(
    job_id: str,
    redis_queue=Depends(get_redis_queue),
) -> DlqActionResult:
    """Permanently remove a DLQ entry. Use for jobs that should never
    be retried (e.g., a misconfigured subscriber that no longer
    exists)."""
    if redis_queue is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Notifier queue not configured (no Redis).",
        )
    ok = redis_queue.delete_from_dlq(job_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No DLQ entry with job_id={job_id!r}",
        )
    logger.info("DLQ entry deleted", extra={"job_id": job_id})
    return DlqActionResult(
        job_id=job_id,
        action="delete",
        success=True,
        message="Job permanently deleted from DLQ.",
    )
