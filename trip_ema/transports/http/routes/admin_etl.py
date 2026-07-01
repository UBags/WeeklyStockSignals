"""ETL admin routes.

Wraps the runner functions in HTTP endpoints so the PySide GUI's
action buttons have something to call. Each "fire" endpoint:

  1. Submits the runner function to AppState.etl_executor.
  2. Returns 202 Accepted immediately with the run_id (or None if
     the runner skipped, e.g. intraday on a holiday).
  3. The GUI polls GET /v1/admin/etl/status for progress.

A single ETL operation can take minutes (backfill: hours). We never
block the HTTP request on completion.

Concurrency policy:
  - We use a SINGLE shared ThreadPoolExecutor for all ETL actions
    (configured at app startup with max_workers=2 by default). The
    runner itself uses an internal ThreadPoolExecutor for yfinance
    batches; this outer one is just to keep the HTTP request
    non-blocking.
  - With max_workers=2, two ETL actions can run concurrently. Don't
    bump higher — running two backfills at once would hammer yfinance
    and the DB; one backfill + one EoD is fine.

Phase 14.6a: every action endpoint now requires an `exchange` query
parameter. The route validates the exchange (must be known and
configured) before submitting to the executor; on failure, returns
400 with a specific error message. The runner wrappers below receive
`exchange` and pass it through to the runner.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, UTC

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Engine, text

from ....config import Config
from ....etl.exchanges import UnsupportedExchangeError, validate_exchange
from ....etl.runner import (
    recompute_tiers,
    refresh_listings,
    run_backfill,
    run_eod,
    run_intraday,
    unpark_all,
)
from ....etl.status_repo import EtlStatusRepository
from ..dependencies import (
    get_config,
    get_db_engine,
    get_etl_executor,
    require_admin_token,
)
from ..schemas import (
    EtlActionAcceptedPayload,
    EtlParkedListPayload,
    EtlParkedTicker,
    EtlRunSummary,
    EtlStatusPayload,
    EtlTierCounts,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/admin/etl",
    tags=["admin", "etl"],
    dependencies=[Depends(require_admin_token)],
)


def _require_executor(executor):
    """If etl_executor is None at AppState construction time, the
    admin ETL endpoints are effectively disabled. Mirror the behavior
    of the existing admin_token check."""
    if executor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "ETL admin endpoints are disabled. Ensure "
                "AppState.etl_executor is provided at startup."
            ),
        )


def _validate_exchange_or_400(exchange: str, config: Config) -> None:
    """Convert UnsupportedExchangeError to HTTP 400 with the exception
    message as the detail. Called at the top of every action endpoint."""
    try:
        validate_exchange(exchange, config)
    except UnsupportedExchangeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


# ---------- Action endpoints (POST) ---------- #


@router.post("/refresh-listings", response_model=EtlActionAcceptedPayload)
def admin_etl_refresh_listings(
    exchange: str = Query(..., min_length=2, max_length=10),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Pull listings CSVs for `exchange` and sync the tickers table.
    Runs asynchronously; the GUI polls /etl/status for progress."""
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: ETL refresh-listings requested",
        extra={"exchange": exchange},
    )
    executor.submit(_run_refresh_listings_safe, db_engine, config, exchange)
    return EtlActionAcceptedPayload(
        action="refresh-listings", run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"Listings refresh ({exchange}) submitted; "
            f"check /etl/status for progress."
        ),
    )


@router.post("/run-eod", response_model=EtlActionAcceptedPayload)
def admin_etl_run_eod(
    exchange: str = Query(..., min_length=2, max_length=10),
    lookback_days: int = Query(7, ge=1, le=90),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger the daily EoD ETL pass for `exchange`. Runs over all
    active tickers in tier order; typical duration 3-5 minutes for
    the full NSE universe."""
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: ETL run-eod requested",
        extra={"exchange": exchange, "lookback_days": lookback_days},
    )
    executor.submit(_run_eod_safe, db_engine, config, exchange, lookback_days)
    return EtlActionAcceptedPayload(
        action="run-eod", run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"EoD run ({exchange}) submitted with "
            f"lookback_days={lookback_days}."
        ),
    )


# ============================================================
# PHASE-1A-ADMIN-ROUTES: enqueue-run, reset-in-progress, queue-snapshot
# ============================================================
from pydantic import BaseModel, Field       # noqa: E402


class EnqueueRunRequest(BaseModel):
    exchange: str = Field(min_length=2, max_length=10)
    kind: str = Field(pattern="^(intraday|eod)$")


class EnqueueRunResponse(BaseModel):
    result: str   # 'inserted' | 'kind_upgraded' | 'noop_duplicate'
    exchange: str
    kind: str


class ResetInProgressRequest(BaseModel):
    exchange: str = Field(min_length=2, max_length=10)


class ResetInProgressResponse(BaseModel):
    killed: bool
    marker_deleted: bool
    exchange: str


class QueueSnapshotItem(BaseModel):
    exchange: str
    kind: str


class InProgressItem(BaseModel):
    exchange: str
    kind: str
    pid: int | None
    started_at: str


class QueueSnapshotResponse(BaseModel):
    queue: list[QueueSnapshotItem]
    in_progress: list[InProgressItem]


@router.post("/enqueue-run", response_model=EnqueueRunResponse)
def admin_etl_enqueue_run(
    body: EnqueueRunRequest,
    config: Config = Depends(get_config),
) -> EnqueueRunResponse:
    """Enqueue an intraday or eod run for the given exchange onto
    the in-memory RunQueue. The dispatcher picks it up on its next
    poll (typically within 30 seconds, or immediately via the
    queue's wait event)."""
    from ..dependencies import get_run_queue
    _validate_exchange_or_400(body.exchange, config)
    queue = get_run_queue()
    result = queue.enqueue(body.exchange, body.kind)   # type: ignore[attr-defined]
    logger.info(
        "Phase 1a: enqueue-run result=%s exchange=%s kind=%s",
        result, body.exchange, body.kind,
    )
    return EnqueueRunResponse(
        result=result, exchange=body.exchange, kind=body.kind,
    )


@router.post("/reset-in-progress", response_model=ResetInProgressResponse)
def admin_etl_reset_in_progress(
    body: ResetInProgressRequest,
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> ResetInProgressResponse:
    """Force-terminate the currently running batch for `exchange`
    (if any) and delete its update_run_in_progress row.

    Sequence:
      1. Send SIGTERM to the subprocess. Wait up to 10 seconds.
      2. Escalate to SIGKILL if still alive.
      3. Delete the marker row (belt-and-suspenders — dispatcher's
         finally also deletes it).
    """
    from ..dependencies import get_run_dispatcher
    from ....persistence import transaction
    from ....persistence.update_run_in_progress_repo import (
        UpdateRunInProgressRepository,
    )
    _validate_exchange_or_400(body.exchange, config)

    dispatcher = get_run_dispatcher()
    killed = dispatcher.cancel_current()   # type: ignore[attr-defined]

    with transaction(db_engine) as conn:
        n = UpdateRunInProgressRepository(conn).delete(body.exchange)

    logger.warning(
        "Phase 1a: reset-in-progress exchange=%s killed=%s "
        "marker_deleted=%d",
        body.exchange, killed, n,
    )
    return ResetInProgressResponse(
        killed=killed, marker_deleted=(n > 0),
        exchange=body.exchange,
    )


@router.get("/queue-snapshot", response_model=QueueSnapshotResponse)
def admin_etl_queue_snapshot(
    db_engine: Engine = Depends(get_db_engine),
) -> QueueSnapshotResponse:
    """Return the current RunQueue snapshot + all in-progress rows.
    Used by the GUI to display "Queued: NYSE:eod, BSE:intraday" +
    the current run."""
    from ..dependencies import get_run_queue
    from ....persistence.update_run_in_progress_repo import (
        UpdateRunInProgressRepository,
    )

    queue = get_run_queue()
    q_items = queue.snapshot()   # type: ignore[attr-defined]

    with db_engine.connect() as conn:
        in_progress = UpdateRunInProgressRepository(conn).list_all()

    return QueueSnapshotResponse(
        queue=[
            QueueSnapshotItem(exchange=i.exchange, kind=i.kind)
            for i in q_items
        ],
        in_progress=[
            InProgressItem(
                exchange=r.exchange, kind=r.kind, pid=r.pid,
                started_at=r.started_at.isoformat(),
            )
            for r in in_progress
        ],
    )


@router.post("/run-intraday", response_model=EtlActionAcceptedPayload)
def admin_etl_run_intraday(
    exchange: str = Query(..., min_length=2, max_length=10),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger the Tier-1 intraday ETL pass for `exchange`. No-op on
    non-trading days; the runner's holiday check handles that
    internally."""
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: ETL run-intraday requested",
        extra={"exchange": exchange},
    )
    executor.submit(_run_intraday_safe, db_engine, config, exchange)
    return EtlActionAcceptedPayload(
        action="run-intraday", run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"Intraday run ({exchange}) submitted "
            f"(will be no-op on non-trading days)."
        ),
    )


@router.post("/recompute-tiers", response_model=EtlActionAcceptedPayload)
def admin_etl_recompute_tiers(
    exchange: str = Query(..., min_length=2, max_length=10),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Refresh tickers.tier from trailing 30-day ADV for `exchange`.
    Fast (DB-only, no fetching). Runs synchronously enough that we
    could block, but we keep the pattern consistent for the GUI."""
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: ETL recompute-tiers requested",
        extra={"exchange": exchange},
    )
    executor.submit(_recompute_tiers_safe, db_engine, config, exchange)
    return EtlActionAcceptedPayload(
        action="recompute-tiers", run_id=None,
        accepted_at=datetime.now(UTC),
        notes=f"Tier recomputation ({exchange}) submitted.",
    )


@router.post("/unpark-all", response_model=EtlActionAcceptedPayload)
def admin_etl_unpark_all(
    exchange: str = Query(..., min_length=2, max_length=10),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> EtlActionAcceptedPayload:
    """Clear the parked flag for every parked ticker on `exchange`.
    Synchronous — this is a single UPDATE, doesn't need the executor."""
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: ETL unpark-all requested",
        extra={"exchange": exchange},
    )
    count = unpark_all(db_engine, config, exchange=exchange)
    return EtlActionAcceptedPayload(
        action="unpark-all", run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"Unparked {count} {exchange} tickers. "
            f"They will retry on next EoD run."
        ),
    )


@router.post("/backfill", response_model=EtlActionAcceptedPayload)
def admin_etl_backfill(
    exchange: str = Query(..., min_length=2, max_length=10),
    start_date: date = Query(..., description="ISO date, e.g. 2010-01-01"),
    tier: int | None = Query(None, ge=1, le=3),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger historical backfill for `exchange` from `start_date`.
    Optional `tier` filter restricts to one tier; omitted = all tiers."""
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: ETL backfill requested",
        extra={
            "exchange": exchange,
            "start_date": start_date.isoformat(),
            "tier": tier,
        },
    )
    from ....etl.types import Tier
    tier_filter = {Tier(tier)} if tier else None
    executor.submit(
        _run_backfill_safe, db_engine, config, exchange, start_date, tier_filter,
    )
    return EtlActionAcceptedPayload(
        action="backfill", run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"Backfill ({exchange}) submitted from {start_date.isoformat()}"
            + (f" (tier {tier} only)" if tier else " (all tiers)")
            + ". Long-running; can take hours."
        ),
    )


"""
The admin_etl.py imports already cover what we need (Query, status,
date, datetime, timezone, EtlActionAcceptedPayload, executor,
require_admin_token via the router-level dependency).
"""

@router.post("/backfill-shares", response_model=EtlActionAcceptedPayload)
def admin_etl_backfill_shares(
    exchange: str = Query(..., min_length=2, max_length=10),
    start_date: date = Query(
        ...,
        description=(
            "ISO date, e.g. 2016-06-01. Earliest date to fetch "
            "historical shares for."
        ),
    ),
    force: bool = Query(
        False,
        description="Bypass the 80-day skip-if-recent guard.",
    ),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor=Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger historical shares-outstanding backfill for `exchange`.

    Fetches yfinance's `get_shares_full(start=start_date)` per
    ticker and inserts one instrument_shares row per filing-date
    snapshot. Once populated, compute-market-cap produces COMPUTED
    rows over the full backfill range (no APPROX fallback).

    Long-running: ~90 min for a full NSE pass. Returns 202
    immediately; poll /v1/fundamentals/runs?phase=shares_backfill
    for progress.
    """
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: ETL shares backfill requested",
        extra={
            "exchange": exchange,
            "start_date": start_date.isoformat(),
            "force": force,
        },
    )
    executor.submit(
        _run_shares_backfill_safe,
        db_engine, exchange, start_date, force,
    )
    return EtlActionAcceptedPayload(
        action="backfill-shares", run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"Shares backfill ({exchange}) submitted from "
            f"{start_date.isoformat()}. Long-running; ~90 minutes. "
            f"Poll /v1/fundamentals/runs?phase=shares_backfill for "
            f"progress."
        ),
    )


# ---------- Status endpoints (GET) ---------- #


@router.get("/status", response_model=EtlStatusPayload)
def admin_etl_status(
    limit: int = Query(20, ge=1, le=100),
    exchange: str | None = Query(
        None, min_length=2, max_length=10,
        description=(
            "Optional exchange code (e.g. NSE). When provided, "
            "tier_counts reflect only that exchange's tickers. "
            "When omitted, falls back to config.etl.exchange for "
            "backward compatibility. The recent_runs list and "
            "parked_count are NOT filtered — they remain global."
        ),
    ),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> EtlStatusPayload:
    """Composite read for the GUI's ETL Status tab. Returns recent
    runs, the current parked count, and tier counts. Cheap — one
    transaction with three queries.

    The GUI polls this every 5s by default.

    Phase 14.6c: accepts an optional `?exchange=` query parameter
    that scopes the tier_counts to one exchange. The recent_runs
    and parked_count are intentionally LEFT global so the operator
    sees activity across all exchanges in the runs table even when
    the picker is on a specific one. (If per-exchange runs filtering
    is ever needed, it's a separate parameter — keep this one's
    semantics focused on tier_counts only.)

    Validation: an unknown or unconfigured exchange falls back
    silently to config.etl.exchange rather than 400ing. The GUI's
    picker only offers exchanges from /v1/admin/exchanges, so a bad
    value here would mean the operator hit the URL directly with a
    typo; returning data for a different exchange is friendlier than
    a hard error in that case. Strict callers should pre-validate.
    """
    effective_exchange = exchange or config.etl.exchange

    with db_engine.connect() as conn:
        status_repo = EtlStatusRepository(conn)
        recent = status_repo.list_recent_runs(limit=limit)
        parked_count = status_repo.count_parked()
        tier_counts = _tier_counts(conn, effective_exchange)

    return EtlStatusPayload(
        recent_runs=[
            EtlRunSummary(
                run_id=r.run_id, kind=r.kind, started_at=r.started_at,
                finished_at=r.finished_at, outcome=r.outcome,
                tickers_attempted=r.tickers_attempted,
                tickers_succeeded=r.tickers_succeeded,
                tickers_failed=r.tickers_failed,
                bars_written=r.bars_written,
                correlation_id=r.correlation_id, notes=r.notes,
            )
            for r in recent
        ],
        parked_count=parked_count,
        tier_counts=tier_counts,
        snapshot_taken_at=datetime.now(UTC),
    )


@router.get("/parked", response_model=EtlParkedListPayload)
def admin_etl_parked(
    db_engine: Engine = Depends(get_db_engine),
) -> EtlParkedListPayload:
    """List currently parked tickers with their last error. Used by
    the parked-tickers panel in the GUI."""
    parked_rows = []
    with db_engine.connect() as conn:
        # Join etl_ticker_status with tickers to get the symbol.
        result = conn.execute(text("""
            SELECT s.ticker_id, t.symbol, s.parked_at, s.parked_reason,
                   s.consecutive_failures, s.last_error
            FROM etl_ticker_status s
            JOIN tickers t ON s.ticker_id = t.ticker_id
            WHERE s.parked = TRUE
            ORDER BY s.parked_at DESC NULLS LAST
        """))
        for r in result:
            parked_rows.append(EtlParkedTicker(
                ticker_id=r.ticker_id, symbol=r.symbol,
                parked_at=r.parked_at,
                parked_reason=r.parked_reason,
                consecutive_failures=r.consecutive_failures,
                last_error=r.last_error,
            ))
    return EtlParkedListPayload(
        parked=parked_rows, total=len(parked_rows),
        snapshot_taken_at=datetime.now(UTC),
    )


# ---------- Internal runner wrappers (catch + log) ---------- #


def _tier_counts(conn, exchange: str) -> EtlTierCounts:
    """How many active tickers in each tier."""
    result = conn.execute(text("""
        SELECT tier, COUNT(*) AS n FROM tickers
        WHERE is_active = TRUE AND delisted_at IS NULL
          AND exchange = :ex
        GROUP BY tier
    """), {"ex": exchange})
    counts = {1: 0, 2: 0, 3: 0}
    for row in result:
        counts[row.tier] = row.n
    return EtlTierCounts(
        tier_1=counts.get(1, 0),
        tier_2=counts.get(2, 0),
        tier_3=counts.get(3, 0),
    )


# These wrappers run inside the ThreadPoolExecutor. They catch every
# exception so a failed runner doesn't pollute the executor; the
# runner itself writes outcome='failed' to etl_runs on errors, so
# the GUI sees the failure naturally via /etl/status.
#
# Note: the exchange parameter is positional in the wrapper signatures
# (positions 3+) for clarity in admin_etl tests that inspect args.


def _run_refresh_listings_safe(db_engine, config, exchange):
    try:
        refresh_listings(db_engine, config, exchange=exchange)
    except Exception:
        logger.exception(
            "Background refresh-listings failed", extra={"exchange": exchange},
        )


def _run_eod_safe(db_engine, config, exchange, lookback_days):
    try:
        run_eod(
            db_engine, config,
            exchange=exchange, days_lookback=lookback_days,
        )
    except Exception:
        logger.exception(
            "Background run-eod failed", extra={"exchange": exchange},
        )


def _run_intraday_safe(db_engine, config, exchange):
    try:
        run_intraday(db_engine, config, exchange=exchange)
    except Exception:
        logger.exception(
            "Background run-intraday failed", extra={"exchange": exchange},
        )


def _recompute_tiers_safe(db_engine, config, exchange):
    try:
        recompute_tiers(db_engine, config, exchange=exchange)
    except Exception:
        logger.exception(
            "Background recompute-tiers failed", extra={"exchange": exchange},
        )


def _run_backfill_safe(db_engine, config, exchange, start_date, tier_filter):
    try:
        run_backfill(
            db_engine, config,
            exchange=exchange,
            start_date=start_date, tier_filter=tier_filter,
        )
    except Exception:
        logger.exception(
            "Background backfill failed", extra={"exchange": exchange},
        )

def _run_shares_backfill_safe(
    db_engine, exchange: str, start_date: date, force: bool,
) -> None:
    """Wrap run_shares_backfill in a try/except that logs and
    swallows. Runs on the etl_executor thread; uncaught exceptions
    here become noisy executor tracebacks with no operator context."""
    try:
        from ....fundamentals.runners import run_shares_backfill
        run_shares_backfill(
            db_engine, exchange,
            start_date=start_date, force=force,
        )
    except Exception:
        logger.exception(
            "run_shares_backfill failed for %s from %s",
            exchange, start_date,
        )