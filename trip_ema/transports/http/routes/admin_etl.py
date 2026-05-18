"""ETL admin routes.

Wraps the Slice 1 runner functions in HTTP endpoints so the PySide
GUI's action buttons have something to call. Each "fire" endpoint:

  1. Submits the runner function to AppState.etl_executor.
  2. Returns 202 Accepted immediately with the run_id (or None if
     the runner skipped, e.g. intraday on a holiday).
  3. The GUI polls GET /v1/admin/etl/status for progress.

A single ETL operation can take minutes (backfill: hours). We never
block the HTTP request on completion.

Concurrency policy:
  - We use a SINGLE shared ThreadPoolExecutor for all ETL actions
    (configured at app startup with max_workers=2 by default). The
    Slice 1 runner itself uses an internal ThreadPoolExecutor for
    yfinance batches; this outer one is just to keep the HTTP request
    non-blocking.
  - With max_workers=2, two ETL actions can run concurrently. Don't
    bump higher — running two backfills at once would hammer yfinance
    and the DB; one backfill + one EoD is fine.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Engine, text

from ....config import Config
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


# ---------- Action endpoints (POST) ---------- #


@router.post("/refresh-listings", response_model=EtlActionAcceptedPayload)
def admin_etl_refresh_listings(
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Pull NSE listings CSVs and sync the tickers table. Runs
    asynchronously; the GUI polls /etl/status for progress."""
    _require_executor(executor)
    logger.info("Admin: ETL refresh-listings requested")
    # We can't get the run_id without running the function; the runner
    # opens the etl_runs row internally. Return None and let the GUI
    # find the new run via the status endpoint (which lists recent runs).
    executor.submit(_run_refresh_listings_safe, db_engine, config)
    return EtlActionAcceptedPayload(
        action="refresh-listings", run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes="Listings refresh submitted; check /etl/status for progress.",
    )


@router.post("/run-eod", response_model=EtlActionAcceptedPayload)
def admin_etl_run_eod(
    lookback_days: int = Query(7, ge=1, le=90),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger the daily EoD ETL pass. Runs over all active tickers
    in tier order; typical duration 3-5 minutes for the full NSE
    universe."""
    _require_executor(executor)
    logger.info("Admin: ETL run-eod requested", extra={"lookback_days": lookback_days})
    executor.submit(_run_eod_safe, db_engine, config, lookback_days)
    return EtlActionAcceptedPayload(
        action="run-eod", run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes=f"EoD run submitted with lookback_days={lookback_days}.",
    )


@router.post("/run-intraday", response_model=EtlActionAcceptedPayload)
def admin_etl_run_intraday(
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger the Tier-1 intraday ETL pass. No-op on non-trading
    days; the runner's holiday check handles that internally."""
    _require_executor(executor)
    logger.info("Admin: ETL run-intraday requested")
    executor.submit(_run_intraday_safe, db_engine, config)
    return EtlActionAcceptedPayload(
        action="run-intraday", run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes="Intraday run submitted (will be no-op on non-trading days).",
    )


@router.post("/recompute-tiers", response_model=EtlActionAcceptedPayload)
def admin_etl_recompute_tiers(
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Refresh tickers.tier from trailing 30-day ADV. Fast (DB-only,
    no fetching). Runs synchronously enough that we could block, but
    we keep the pattern consistent for the GUI."""
    _require_executor(executor)
    logger.info("Admin: ETL recompute-tiers requested")
    executor.submit(_recompute_tiers_safe, db_engine, config)
    return EtlActionAcceptedPayload(
        action="recompute-tiers", run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes="Tier recomputation submitted.",
    )


@router.post("/unpark-all", response_model=EtlActionAcceptedPayload)
def admin_etl_unpark_all(
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> EtlActionAcceptedPayload:
    """Clear the parked flag for every parked ticker. Synchronous —
    this is a single UPDATE, doesn't need the executor."""
    logger.info("Admin: ETL unpark-all requested")
    count = unpark_all(db_engine, config)
    return EtlActionAcceptedPayload(
        action="unpark-all", run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes=f"Unparked {count} tickers. They will retry on next EoD run.",
    )


@router.post("/backfill", response_model=EtlActionAcceptedPayload)
def admin_etl_backfill(
    start_date: date = Query(..., description="ISO date, e.g. 2010-01-01"),
    tier: int | None = Query(None, ge=1, le=3),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger historical backfill from `start_date`. Optional `tier`
    filter restricts to one tier; omitted = all tiers."""
    _require_executor(executor)
    logger.info(
        "Admin: ETL backfill requested",
        extra={"start_date": start_date.isoformat(), "tier": tier},
    )
    from ....etl.types import Tier
    tier_filter = {Tier(tier)} if tier else None
    executor.submit(_run_backfill_safe, db_engine, config, start_date, tier_filter)
    return EtlActionAcceptedPayload(
        action="backfill", run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes=(
            f"Backfill submitted from {start_date.isoformat()}"
            + (f" (tier {tier} only)" if tier else " (all tiers)")
            + ". Long-running; can take hours."
        ),
    )


# ---------- Status endpoints (GET) ---------- #


@router.get("/status", response_model=EtlStatusPayload)
def admin_etl_status(
    limit: int = Query(20, ge=1, le=100),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> EtlStatusPayload:
    """Composite read for the GUI's ETL Status tab. Returns recent
    runs, the current parked count, and tier counts. Cheap — one
    transaction with three queries.

    The GUI polls this every 5s by default."""
    with db_engine.connect() as conn:
        status_repo = EtlStatusRepository(conn)
        recent = status_repo.list_recent_runs(limit=limit)
        parked_count = status_repo.count_parked()
        tier_counts = _tier_counts(conn, config.etl.exchange)

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
        snapshot_taken_at=datetime.now(timezone.utc),
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
        snapshot_taken_at=datetime.now(timezone.utc),
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


def _run_refresh_listings_safe(db_engine, config):
    try:
        refresh_listings(db_engine, config)
    except Exception:
        logger.exception("Background refresh-listings failed")


def _run_eod_safe(db_engine, config, lookback_days):
    try:
        run_eod(db_engine, config, days_lookback=lookback_days)
    except Exception:
        logger.exception("Background run-eod failed")


def _run_intraday_safe(db_engine, config):
    try:
        run_intraday(db_engine, config)
    except Exception:
        logger.exception("Background run-intraday failed")


def _recompute_tiers_safe(db_engine, config):
    try:
        recompute_tiers(db_engine, config)
    except Exception:
        logger.exception("Background recompute-tiers failed")


def _run_backfill_safe(db_engine, config, start_date, tier_filter):
    try:
        run_backfill(
            db_engine, config,
            start_date=start_date, tier_filter=tier_filter,
        )
    except Exception:
        logger.exception("Background backfill failed")
