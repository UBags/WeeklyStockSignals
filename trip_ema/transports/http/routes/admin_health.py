"""System-health snapshot endpoint (Slice 4).

  GET /v1/admin/health-snapshot

Returns a list of "cards" — one per logical subsystem — each with a
status (ok/warn/crit/unknown), a one-liner headline, and an optional
detail string. The GUI's System Health dashboard renders these as a
grid of color-coded cards.

The cards are computed cheaply: one SQL query for DB-derived counts,
some in-process state reads for the cache + scheduler + circuit
breakers. No new background work introduced — the snapshot is read-
only and side-effect-free.
"""

from __future__ import annotations

import logging
from datetime import datetime, UTC

from fastapi import APIRouter, Depends
from sqlalchemy import Engine, text

# PHASE-3E: SignalCache + get_cache imports removed (subsystem deleted).
from ..dependencies import (
    get_db_engine,
    get_redis_queue,
    require_admin_token,
)
from ..schemas import HealthCardPayload, HealthSnapshotPayload

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/admin/health-snapshot",
    tags=["admin", "health"],
    dependencies=[Depends(require_admin_token)],
)


# Status precedence: crit > warn > ok > unknown.
_STATUS_RANK = {"crit": 3, "warn": 2, "ok": 1, "unknown": 0}


# PHASE-3E: cache dep removed (SignalCache deleted).
@router.get("", response_model=HealthSnapshotPayload)
def health_snapshot(
    db_engine: Engine = Depends(get_db_engine),
    redis_queue=Depends(get_redis_queue),
) -> HealthSnapshotPayload:
    """One call returning every dashboard card. ~10ms — composed of
    several cheap reads (one SQL query, several in-process state
    snapshots)."""
    cards: list[HealthCardPayload] = []

    # 1. DB ticker counts (single query — both active and parked)
    cards.append(_db_universe_card(db_engine))

    # 2. ETL freshness (any active run, last successful EoD age)
    cards.append(_etl_freshness_card(db_engine))

    # PHASE-3B: compute step health now derives from compute_runs table.
    cards.append(_compute_scheduler_card(db_engine))

    # PHASE-3E: _cache_card removed (SignalCache deleted; empty by construction).

    # 5. Circuit breakers — legacy in-process distributor was demolished
    cards.append(_circuit_breakers_card(redis_queue))

    # 6. Notification queue depth (Redis mode) / pending notifications
    cards.append(_notification_queue_card(redis_queue))

    # 7. Subscriber stats
    cards.append(_subscribers_card(db_engine))

    # === STEP 15-B9-call: _recent_events_card removed ===

    overall = _aggregate_overall_status(cards)
    return HealthSnapshotPayload(
        overall_status=overall, cards=cards,
        snapshot_taken_at=datetime.now(UTC),
    )


# ---------- per-card builders ---------- #


def _db_universe_card(db_engine: Engine) -> HealthCardPayload:
    """Active / delisted / parked ticker counts. Warn if no active
    tickers; crit if DB unreachable."""
    try:
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE is_active = TRUE AND delisted_at IS NULL) AS active,
                    COUNT(*) FILTER (WHERE delisted_at IS NOT NULL) AS delisted
                FROM tickers
            """)).first()
            parked = conn.execute(text("""
                SELECT COUNT(*) AS n FROM etl_ticker_status WHERE parked = TRUE
            """)).scalar() or 0
        if row is None:
            return HealthCardPayload(
                name="Ticker universe",
                status="unknown",
                headline="Could not query ticker registry",
                detail="The tickers query returned no rows.",
            )
        active = row.active or 0
        delisted = row.delisted or 0
        if active == 0:
            return HealthCardPayload(
                name="Ticker universe",
                status="warn",
                headline="No active tickers",
                detail="Run trip-ema-etl refresh-listings to populate.",
            )
        return HealthCardPayload(
            name="Ticker universe",
            status="warn" if parked > 0 else "ok",
            headline=f"{active:,} active tickers",
            detail=f"{delisted} delisted, {parked} parked",
        )
    except Exception as e:
        return HealthCardPayload(
            name="Ticker universe", status="crit",
            headline="DB error",
            detail=f"Could not query tickers: {e}",
        )


def _etl_freshness_card(db_engine: Engine) -> HealthCardPayload:
    """How long since the last successful EoD ETL run? Warn at >36h,
    crit at >7d. -1 (no row) → unknown."""
    try:
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT MAX(finished_at) AS last_finished, COUNT(*) AS total_runs
                FROM etl_runs
                WHERE kind = 'eod' AND outcome IN ('success', 'partial')
            """)).first()
        if row is None or row.last_finished is None:
            return HealthCardPayload(
                name="ETL freshness", status="unknown",
                headline="No EoD runs recorded",
                detail="Run trip-ema-etl incremental-eod to seed.",
            )
        age = datetime.now(UTC) - row.last_finished
        hours = age.total_seconds() / 3600.0
        if hours > 24 * 7:
            return HealthCardPayload(
                name="ETL freshness", status="crit",
                headline=f"Last EoD: {hours / 24:.1f}d ago",
                detail=(
                    f"Stale. {row.total_runs} runs total; last finished "
                    f"{row.last_finished.isoformat(timespec='seconds')}."
                ),
            )
        if hours > 36:
            return HealthCardPayload(
                name="ETL freshness", status="warn",
                headline=f"Last EoD: {hours:.1f}h ago",
                detail=f"Expected within 24h; {row.total_runs} runs total.",
            )
        return HealthCardPayload(
            name="ETL freshness", status="ok",
            headline=f"Last EoD: {hours:.1f}h ago",
            detail=f"{row.total_runs} runs total.",
        )
    except Exception as e:
        return HealthCardPayload(
            name="ETL freshness", status="crit",
            headline="Query error", detail=str(e),
        )


# === STEP 14B-FIXUP2-cs_card: scheduler arg + legacy branch removed ===
def _compute_scheduler_card(
    db_engine: Engine,
) -> HealthCardPayload:
    """Compute step health, derived from `compute_runs`.

    PHASE-3B rewrite: MarketOrchestrator + its `last_run_summary` API
    are gone. We derive the same card from the most-recent finished
    row in `compute_runs`. Cheap: single indexed lookup by finished_at.

    Status mapping:
      finished_at IS NULL rows are in-flight — not counted here.
      failed_stage IS NULL          -> ok    (clean success)
      failed_stage = 'cancelled'    -> warn  (SIGTERM by operator)
      failed_stage = 'boot_orphan_cleanup' -> warn (prior crash)
      other non-null failed_stage   -> crit  (real failure)
    """
    try:
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT exchange, kind, started_at, finished_at,
                       failed_stage, error_message
                  FROM compute_runs
                 WHERE finished_at IS NOT NULL
                 ORDER BY finished_at DESC
                 LIMIT 1
            """)).first()
        if row is None:
            return HealthCardPayload(
                name="Compute scheduler", status="unknown",
                headline="Never run",
                detail="No finished compute_runs rows yet.",
            )

        age_seconds = (datetime.now(UTC) - row.finished_at).total_seconds()
        age_str = (
            f"{age_seconds / 60:.1f} min ago"
            if age_seconds < 3600
            else f"{age_seconds / 3600:.1f}h ago"
        )
        label = f"{row.exchange} {row.kind}"

        if row.failed_stage is None:
            duration = (row.finished_at - row.started_at).total_seconds()
            return HealthCardPayload(
                name="Compute scheduler", status="ok",
                headline=f"Last cycle ({label}): {age_str}",
                detail=f"Succeeded in {duration:.1f}s.",
            )

        if row.failed_stage in ("cancelled", "boot_orphan_cleanup"):
            return HealthCardPayload(
                name="Compute scheduler", status="warn",
                headline=f"Last cycle ({label}): {age_str}",
                detail=(
                    row.error_message
                    or f"Cycle ended with failed_stage={row.failed_stage!r}."
                ),
            )

        return HealthCardPayload(
            name="Compute scheduler", status="crit",
            headline=f"Last cycle ({label}): {age_str} — FAILED",
            detail=row.error_message or f"failed_stage={row.failed_stage!r}",
        )
    except Exception as e:
        return HealthCardPayload(
            name="Compute scheduler", status="unknown",
            headline="compute_runs query failed", detail=str(e),
        )

# PHASE-3E: _cache_card deleted. No consumer; SignalCache no longer exists.


def _circuit_breakers_card(redis_queue) -> HealthCardPayload:
    """Open breakers = some transport is failing repeatedly.
    Queue mode (Redis): breakers live in the notifier worker process.
    In-process distributor was demolished in Step 14b — V1 sends go
    through notify_subscribers without breakers (Brevo's own
    rate-limiting + retry handles transient failures).
    """
    if redis_queue is not None:
        return HealthCardPayload(
            name="Circuit breakers", status="unknown",
            headline="Queue mode — breakers in notifier worker",
            detail="Inspect via the notifier worker's metrics.",
        )
    return HealthCardPayload(
        name="Circuit breakers", status="ok",
        headline="V1 in-process sends (no breakers)",
        detail=(
            "Legacy in-process distributor was demolished in Step "
            "14b. V1 notify_subscribers uses Brevo's own retry."
        ),
    )


# === STEP 14B-FIXUP2-nq_card: distributor arg removed ===
def _notification_queue_card(redis_queue) -> HealthCardPayload:
    """Queue mode: depth of the redis list. Legacy: nothing to show."""
    if redis_queue is None:
        return HealthCardPayload(
            name="Notification queue", status="ok",
            headline="Legacy mode (in-process)",
            detail="Distributor sends inline; no queue.",
        )
    try:
        depth = redis_queue.depth() if hasattr(redis_queue, "depth") else None
        if depth is None:
            return HealthCardPayload(
                name="Notification queue", status="unknown",
                headline="Depth not exposed",
            )
        if depth > 5000:
            return HealthCardPayload(
                name="Notification queue", status="crit",
                headline=f"{depth:,} jobs queued",
                detail="Backlog growing — notifier worker may be down.",
            )
        if depth > 500:
            return HealthCardPayload(
                name="Notification queue", status="warn",
                headline=f"{depth:,} jobs queued",
            )
        return HealthCardPayload(
            name="Notification queue", status="ok",
            headline=f"{depth:,} jobs queued",
        )
    except Exception as e:
        return HealthCardPayload(
            name="Notification queue", status="unknown",
            headline="Redis unreachable", detail=str(e),
        )


def _subscribers_card(db_engine: Engine) -> HealthCardPayload:
    try:
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE is_active = TRUE) AS active,
                    COUNT(*) AS total
                FROM subscribers
            """)).first()
        if row is None:
            return HealthCardPayload(
                name="Subscribers",
                status="unknown",
                headline="Could not query subscribers",
                detail="The subscribers query returned no rows.",
            )
        if row.total == 0:
            return HealthCardPayload(
                name="Subscribers", status="warn",
                headline="No subscribers configured",
                detail="Add some via the Subscribers tab.",
            )
        return HealthCardPayload(
            name="Subscribers", status="ok",
            headline=f"{row.active}/{row.total} active",
        )
    except Exception as e:
        return HealthCardPayload(
            name="Subscribers", status="unknown",
            headline="Query failed", detail=str(e),
        )


# === STEP 15-B9-fn: _recent_events_card removed ===




def _aggregate_overall_status(cards: list[HealthCardPayload]) -> str:
    """Worst-of-all across cards."""
    if not cards:
        return "unknown"
    return max(cards, key=lambda c: _STATUS_RANK.get(c.status, 0)).status
