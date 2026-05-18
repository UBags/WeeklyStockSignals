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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import Engine, text

from ....cache import SignalCache
from ....notifications.circuit_breaker import CircuitBreakingTransport
from ....registry import TickerRegistry
from ....scheduler import ComputeScheduler, SubscriberDistributor
from ..dependencies import (
    get_cache, get_compute_scheduler, get_db_engine, get_distributor,
    get_redis_queue, get_registry, require_admin_token,
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


@router.get("", response_model=HealthSnapshotPayload)
def health_snapshot(
    db_engine: Engine = Depends(get_db_engine),
    registry: TickerRegistry = Depends(get_registry),
    cache: SignalCache = Depends(get_cache),
    compute_scheduler: ComputeScheduler = Depends(get_compute_scheduler),
    distributor: SubscriberDistributor | None = Depends(get_distributor),
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

    # 3. Compute scheduler — is it running, when did it last complete
    cards.append(_compute_scheduler_card(compute_scheduler))

    # 4. Cache — populated entry count vs registered ticker count
    cards.append(_cache_card(cache, registry))

    # 5. Circuit breakers — pulled from the distributor in legacy mode
    cards.append(_circuit_breakers_card(distributor, redis_queue))

    # 6. Notification queue depth (Redis mode) / pending notifications
    cards.append(_notification_queue_card(distributor, redis_queue))

    # 7. Subscriber stats
    cards.append(_subscribers_card(db_engine))

    # 8. Recent signal-event volume
    cards.append(_recent_events_card(db_engine))

    overall = _aggregate_overall_status(cards)
    return HealthSnapshotPayload(
        overall_status=overall, cards=cards,
        snapshot_taken_at=datetime.now(timezone.utc),
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
        age = datetime.now(timezone.utc) - row.last_finished
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


def _compute_scheduler_card(scheduler: ComputeScheduler) -> HealthCardPayload:
    """The ComputeScheduler runs every N minutes. Report when it last
    completed and whether the next run is overdue."""
    try:
        last = getattr(scheduler, "last_run_completed_at", None)
        if last is None:
            return HealthCardPayload(
                name="Compute scheduler", status="unknown",
                headline="Never run",
                detail="Scheduler hasn't completed a cycle yet.",
            )
        age_seconds = (datetime.now(timezone.utc) - last).total_seconds()
        if age_seconds > 3600:
            return HealthCardPayload(
                name="Compute scheduler", status="warn",
                headline=f"Last cycle: {age_seconds / 60:.0f} min ago",
                detail="Expected within 60 min.",
            )
        return HealthCardPayload(
            name="Compute scheduler", status="ok",
            headline=f"Last cycle: {age_seconds / 60:.1f} min ago",
        )
    except Exception as e:
        return HealthCardPayload(
            name="Compute scheduler", status="unknown",
            headline="State unavailable", detail=str(e),
        )


def _cache_card(cache: SignalCache, registry: TickerRegistry) -> HealthCardPayload:
    """Cache should hold one entry per registered ticker × active
    timeframe. Mismatch indicates priming failed for some tickers."""
    try:
        # Use whatever introspection the cache offers; fall back to
        # 'unknown' if neither cache nor registry exposes a count.
        n_entries = getattr(cache, "size", lambda: None)()
        n_registered = len(getattr(registry, "ticker_refs", []) or [])
        if n_entries is None:
            return HealthCardPayload(
                name="Signal cache", status="unknown",
                headline="Cache size not exposed",
                detail=f"{n_registered} tickers registered.",
            )
        if n_registered == 0:
            return HealthCardPayload(
                name="Signal cache", status="unknown",
                headline="No tickers registered",
                detail="Run prime-all to populate.",
            )
        coverage = (n_entries / n_registered) if n_registered else 0.0
        if coverage < 0.5:
            return HealthCardPayload(
                name="Signal cache", status="warn",
                headline=f"{n_entries:,} entries / {n_registered:,} tickers",
                detail=f"Coverage {coverage * 100:.0f}% — priming may have failed.",
            )
        return HealthCardPayload(
            name="Signal cache", status="ok",
            headline=f"{n_entries:,} entries / {n_registered:,} tickers",
            detail=f"Coverage {coverage * 100:.0f}%.",
        )
    except Exception as e:
        return HealthCardPayload(
            name="Signal cache", status="unknown",
            headline="State unavailable", detail=str(e),
        )


def _circuit_breakers_card(distributor, redis_queue) -> HealthCardPayload:
    """Open breakers = some transport is failing repeatedly.
    Queue mode: we can't see breakers from this process; report unknown."""
    if redis_queue is not None:
        return HealthCardPayload(
            name="Circuit breakers", status="unknown",
            headline="Queue mode — breakers in notifier worker",
            detail="Inspect via the notifier worker's metrics.",
        )
    if distributor is None:
        return HealthCardPayload(
            name="Circuit breakers", status="unknown",
            headline="Distributor not present",
        )
    try:
        breakers = getattr(distributor, "transports", {}) or {}
        # Each transport may be a CircuitBreakingTransport — call .state
        # if it exists.
        open_count = 0
        states = []
        for name, t in breakers.items():
            state_attr = getattr(t, "state", None)
            if state_attr:
                state_val = state_attr() if callable(state_attr) else state_attr
                states.append(f"{name}: {state_val}")
                if str(state_val).lower() == "open":
                    open_count += 1
        if open_count > 0:
            return HealthCardPayload(
                name="Circuit breakers", status="crit",
                headline=f"{open_count} open breaker(s)",
                detail=", ".join(states) or "see /v1/admin/circuit-breakers",
            )
        return HealthCardPayload(
            name="Circuit breakers", status="ok",
            headline=(f"{len(breakers)} transport(s), all closed"
                      if breakers else "No transports configured"),
            detail=", ".join(states) if states else None,
        )
    except Exception as e:
        return HealthCardPayload(
            name="Circuit breakers", status="unknown",
            headline="Inspection failed", detail=str(e),
        )


def _notification_queue_card(distributor, redis_queue) -> HealthCardPayload:
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


def _recent_events_card(db_engine: Engine) -> HealthCardPayload:
    """Signal events in the last 7 days. 'ok' regardless of count —
    this is informational."""
    try:
        with db_engine.connect() as conn:
            count = conn.execute(text("""
                SELECT COUNT(*) FROM signal_events
                WHERE computed_at >= NOW() - INTERVAL '7 days'
            """)).scalar() or 0
        return HealthCardPayload(
            name="Signal events (7d)", status="ok",
            headline=f"{count:,} events",
            detail="Generated in the last 7 days.",
        )
    except Exception as e:
        return HealthCardPayload(
            name="Signal events (7d)", status="unknown",
            headline="Query failed", detail=str(e),
        )


def _aggregate_overall_status(cards: list[HealthCardPayload]) -> str:
    """Worst-of-all across cards."""
    if not cards:
        return "unknown"
    return max(cards, key=lambda c: _STATUS_RANK.get(c.status, 0)).status
