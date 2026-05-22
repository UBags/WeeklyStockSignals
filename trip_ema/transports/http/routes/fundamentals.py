"""Fundamentals admin + read routes (Phase 1).

Endpoints:

  Reads (gated by admin token, but informational):
    GET    /v1/fundamentals/industry             — list classifications by exchange
    GET    /v1/fundamentals/industry/{ticker_id} — one row
    GET    /v1/fundamentals/coverage             — coverage stats
    GET    /v1/fundamentals/runs                 — recent fundamentals_run rows

  Writes (admin token):
    POST   /v1/fundamentals/refresh-industry        — kick a refresh
    POST   /v1/fundamentals/override-industry       — set operator override
    DELETE /v1/fundamentals/override-industry/{tid} — remove operator override

The POST refresh endpoint is async: it submits to AppState.etl_executor
and returns 202 immediately with a run_id. The GUI polls /runs for
progress, identical to the existing /admin/etl pattern.

We reuse the etl_executor instead of creating a separate fundamentals
executor because the workload shape is the same (one long-running
function per request) and we want a single global concurrency limit
to keep yfinance + DB from getting overwhelmed.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from ....config import Config
from ....etl.exchanges import UnsupportedExchangeError, validate_exchange
from ....fundamentals.runners import run_industry_refresh
from ....persistence import transaction
from ....persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndustryOverrideRepository,
    InstrumentIndustryRepository,
)
from ..dependencies import (
    get_config,
    get_db_engine,
    get_etl_executor,
    require_admin_token,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/fundamentals",
    tags=["fundamentals"],
    dependencies=[Depends(require_admin_token)],
)


# ---------- response/request schemas ---------- #


class IndustryRowPayload(BaseModel):
    ticker_id: int
    sector: Optional[str] = None
    industry: Optional[str] = None
    classification_source: str
    fetched_at: datetime
    updated_at: datetime


class IndustryListPayload(BaseModel):
    exchange: str
    rows: list[IndustryRowPayload]


class CoverageRowPayload(BaseModel):
    exchange: str
    total: int
    classified: int
    pct: float


class CoveragePayload(BaseModel):
    rows: list[CoverageRowPayload]


class RunRowPayload(BaseModel):
    run_id: int
    phase: str
    exchange: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str
    succeeded: int
    failed: int
    skipped: int


class RunListPayload(BaseModel):
    rows: list[RunRowPayload]


class RefreshAcceptedPayload(BaseModel):
    action: str
    run_id: Optional[int] = None
    accepted_at: datetime
    notes: str


class OverrideRequestPayload(BaseModel):
    ticker_id: int
    sector: str = "Unclassified"
    industry: str = Field(..., min_length=1, max_length=128)
    note: Optional[str] = None
    set_by: Optional[str] = None


class OverrideAckPayload(BaseModel):
    ticker_id: int
    status: str  # "set" or "removed"


# ---------- helpers ---------- #


def _require_executor(executor):
    if executor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Fundamentals admin endpoints are disabled. Ensure "
                "AppState.etl_executor is provided at startup."
            ),
        )


def _validate_exchange_or_400(exchange: str, config: Config) -> None:
    try:
        validate_exchange(exchange, config)
    except UnsupportedExchangeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


def _run_industry_refresh_safe(
    db_engine: Engine, config: Config, exchange: str, force: bool,
) -> None:
    """Wrapper that runs in the executor — catches exceptions so the
    executor thread doesn't die silently. Errors are logged; the
    run row's status field carries the real outcome."""
    try:
        run_industry_refresh(db_engine, exchange, force=force)
    except Exception:
        logger.exception(
            "Industry refresh failed for exchange=%s", exchange,
        )


# ---------- read endpoints ---------- #


@router.get("/industry", response_model=IndustryListPayload)
def list_industry(
    exchange: str = Query(..., min_length=2, max_length=10),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> IndustryListPayload:
    _validate_exchange_or_400(exchange, config)
    with db_engine.connect() as conn:
        rows = InstrumentIndustryRepository(conn).list_for_exchange(exchange)
    return IndustryListPayload(
        exchange=exchange,
        rows=[
            IndustryRowPayload(
                ticker_id=r.ticker_id,
                sector=r.sector,
                industry=r.industry,
                classification_source=r.classification_source,
                fetched_at=r.fetched_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ],
    )


@router.get("/industry/{ticker_id}", response_model=IndustryRowPayload)
def get_industry(
    ticker_id: int,
    db_engine: Engine = Depends(get_db_engine),
) -> IndustryRowPayload:
    with db_engine.connect() as conn:
        row = InstrumentIndustryRepository(conn).get(ticker_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No industry classification for ticker_id={ticker_id}",
        )
    return IndustryRowPayload(
        ticker_id=row.ticker_id,
        sector=row.sector,
        industry=row.industry,
        classification_source=row.classification_source,
        fetched_at=row.fetched_at,
        updated_at=row.updated_at,
    )


@router.get("/coverage", response_model=CoveragePayload)
def get_coverage(
    db_engine: Engine = Depends(get_db_engine),
) -> CoveragePayload:
    with db_engine.connect() as conn:
        counts = InstrumentIndustryRepository(conn).coverage_counts()
    return CoveragePayload(rows=[
        CoverageRowPayload(
            exchange=ex,
            total=total,
            classified=classified,
            pct=(classified * 100.0 / total) if total else 0.0,
        )
        for ex, total, classified in counts
    ])


@router.get("/runs", response_model=RunListPayload)
def list_runs(
    phase: Optional[str] = Query(None),
    exchange: Optional[str] = Query(None, min_length=2, max_length=10),
    limit: int = Query(20, ge=1, le=100),
    db_engine: Engine = Depends(get_db_engine),
) -> RunListPayload:
    with db_engine.connect() as conn:
        rows = FundamentalsRunRepository(conn).list_recent(
            phase=phase, exchange=exchange, limit=limit,
        )
    return RunListPayload(rows=[
        RunRowPayload(
            run_id=r.run_id,
            phase=r.phase,
            exchange=r.exchange,
            started_at=r.started_at,
            completed_at=r.completed_at,
            status=r.status,
            succeeded=r.succeeded,
            failed=r.failed,
            skipped=r.skipped,
        )
        for r in rows
    ])


# ---------- write endpoints ---------- #


@router.post("/refresh-industry", response_model=RefreshAcceptedPayload, status_code=202)
def refresh_industry(
    exchange: str = Query(..., min_length=2, max_length=10),
    force: bool = Query(False),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> RefreshAcceptedPayload:
    """Fire-and-forget: submits the runner to the ETL executor and
    returns 202 immediately. The GUI polls /runs for the resulting
    fundamentals_run row's status."""
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: refresh-industry requested",
        extra={"exchange": exchange, "force": force},
    )
    executor.submit(_run_industry_refresh_safe, db_engine, config, exchange, force)
    return RefreshAcceptedPayload(
        action="refresh-industry",
        run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes=(
            f"Industry refresh ({exchange}, force={force}) submitted; "
            f"check /v1/fundamentals/runs for progress."
        ),
    )


@router.post("/override-industry", response_model=OverrideAckPayload)
def set_override(
    body: OverrideRequestPayload,
    db_engine: Engine = Depends(get_db_engine),
) -> OverrideAckPayload:
    # Verify the ticker_id exists before writing.
    with db_engine.connect() as conn:
        row = conn.execute(text("""
            SELECT ticker_id FROM tickers WHERE ticker_id = :tid
        """), {"tid": body.ticker_id}).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ticker_id={body.ticker_id} not found",
        )
    with transaction(db_engine) as conn:
        IndustryOverrideRepository(conn).upsert(
            body.ticker_id,
            sector=body.sector,
            industry=body.industry,
            note=body.note,
            set_by=body.set_by,
        )
    return OverrideAckPayload(ticker_id=body.ticker_id, status="set")


@router.delete("/override-industry/{ticker_id}", response_model=OverrideAckPayload)
def delete_override(
    ticker_id: int,
    db_engine: Engine = Depends(get_db_engine),
) -> OverrideAckPayload:
    with transaction(db_engine) as conn:
        deleted = IndustryOverrideRepository(conn).delete(ticker_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No override for ticker_id={ticker_id}",
        )
    return OverrideAckPayload(ticker_id=ticker_id, status="removed")


# ============================================================
# Phase 2 — shares-outstanding endpoints
# ============================================================


from ....persistence.fundamentals_repo import InstrumentSharesRepository


class SharesRowPayload(BaseModel):
    ticker_id: int
    as_of_date: "date"
    shares_outstanding: Optional[int] = None
    free_float_factor: Optional[float] = None
    shares_source: str
    fetched_at: datetime
    xbrl_url: Optional[str] = None


class SharesListPayload(BaseModel):
    exchange: str
    rows: list[SharesRowPayload]


class SharesHistoryPayload(BaseModel):
    ticker_id: int
    rows: list[SharesRowPayload]  # newest first


def _run_shares_refresh_safe(
    db_engine: Engine, config: Config, exchange: str, force: bool,
) -> None:
    """Wrapper for the executor — catches exceptions so the executor
    thread doesn't die silently. Errors are logged; the run row
    carries the real outcome."""
    from ....fundamentals.runners import run_shares_refresh
    try:
        run_shares_refresh(db_engine, exchange, force=force)
    except Exception:
        logger.exception(
            "Shares refresh failed for exchange=%s", exchange,
        )


@router.get("/shares", response_model=SharesListPayload)
def list_shares(
    exchange: str = Query(..., min_length=2, max_length=10),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> SharesListPayload:
    """Latest shares-outstanding snapshot per active ticker on
    `exchange`. For historical lookups use /shares/{ticker_id}/history."""
    _validate_exchange_or_400(exchange, config)
    with db_engine.connect() as conn:
        rows = InstrumentSharesRepository(conn).list_latest_for_exchange(exchange)
    return SharesListPayload(
        exchange=exchange,
        rows=[
            SharesRowPayload(
                ticker_id=r.ticker_id,
                as_of_date=r.as_of_date,
                shares_outstanding=r.shares_outstanding,
                free_float_factor=r.free_float_factor,
                shares_source=r.shares_source,
                fetched_at=r.fetched_at,
                xbrl_url=r.xbrl_url,
            )
            for r in rows
        ],
    )


@router.get("/shares/{ticker_id}", response_model=SharesRowPayload)
def get_latest_shares(
    ticker_id: int,
    db_engine: Engine = Depends(get_db_engine),
) -> SharesRowPayload:
    with db_engine.connect() as conn:
        row = InstrumentSharesRepository(conn).get_latest(ticker_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No shares data for ticker_id={ticker_id}",
        )
    return SharesRowPayload(
        ticker_id=row.ticker_id,
        as_of_date=row.as_of_date,
        shares_outstanding=row.shares_outstanding,
        free_float_factor=row.free_float_factor,
        shares_source=row.shares_source,
        fetched_at=row.fetched_at,
        xbrl_url=row.xbrl_url,
    )


@router.get("/shares/{ticker_id}/history", response_model=SharesHistoryPayload)
def get_shares_history(
    ticker_id: int,
    limit: int = Query(100, ge=1, le=1000),
    db_engine: Engine = Depends(get_db_engine),
) -> SharesHistoryPayload:
    """Full shares-outstanding history for one ticker, newest first.

    Used by analytics that need to see splits/buybacks/issues
    reflected in the shares count over time.
    """
    with db_engine.connect() as conn:
        history = InstrumentSharesRepository(conn).list_history(
            ticker_id, limit=limit,
        )
    return SharesHistoryPayload(
        ticker_id=ticker_id,
        rows=[
            SharesRowPayload(
                ticker_id=r.ticker_id,
                as_of_date=r.as_of_date,
                shares_outstanding=r.shares_outstanding,
                free_float_factor=r.free_float_factor,
                shares_source=r.shares_source,
                fetched_at=r.fetched_at,
                xbrl_url=r.xbrl_url,
            )
            for r in history
        ],
    )


@router.post("/refresh-shares", response_model=RefreshAcceptedPayload, status_code=202)
def refresh_shares(
    exchange: str = Query(..., min_length=2, max_length=10),
    force: bool = Query(False),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> RefreshAcceptedPayload:
    """Fire-and-forget shares refresh. Returns 202 immediately;
    poll /runs for the result."""
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: refresh-shares requested",
        extra={"exchange": exchange, "force": force},
    )
    executor.submit(_run_shares_refresh_safe, db_engine, config, exchange, force)
    return RefreshAcceptedPayload(
        action="refresh-shares",
        run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes=(
            f"Shares refresh ({exchange}, force={force}) submitted; "
            f"check /v1/fundamentals/runs for progress."
        ),
    )


# ============================================================
# Phase 3 — quarterly financials endpoints
# ============================================================


from ....persistence.fundamentals_repo import QuarterlyFinancialsRepository


class FinancialRowPayload(BaseModel):
    ticker_id: int
    fiscal_quarter: "date"
    fetched_at: datetime
    revenue: Optional[float] = None
    ebitda: Optional[float] = None
    currency: str
    source: str


class FinancialListPayload(BaseModel):
    exchange: str
    rows: list[FinancialRowPayload]


class FinancialHistoryPayload(BaseModel):
    ticker_id: int
    fiscal_quarter: "date"
    rows: list[FinancialRowPayload]  # newest fetched_at first


class FinancialQuartersPayload(BaseModel):
    """Last N quarters for one ticker, newest quarter first."""
    ticker_id: int
    rows: list[FinancialRowPayload]


def _run_financials_refresh_safe(
    db_engine: Engine, config: Config, exchange: str, force: bool,
) -> None:
    """Executor wrapper — catches exceptions so the executor thread
    doesn't die silently. Errors are logged; run row carries the
    real outcome."""
    from ....fundamentals.runners import run_financials_refresh
    try:
        run_financials_refresh(db_engine, exchange, force=force)
    except Exception:
        logger.exception(
            "Financials refresh failed for exchange=%s", exchange,
        )


def _finrow_payload(r) -> FinancialRowPayload:
    return FinancialRowPayload(
        ticker_id=r.ticker_id,
        fiscal_quarter=r.fiscal_quarter,
        fetched_at=r.fetched_at,
        revenue=r.revenue,
        ebitda=r.ebitda,
        currency=r.currency,
        source=r.source,
    )


@router.get("/financials", response_model=FinancialListPayload)
def list_financials(
    exchange: str = Query(..., min_length=2, max_length=10),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> FinancialListPayload:
    """Latest-quarter snapshot per active ticker on `exchange`. For
    a specific quarter, use ?fiscal_quarter=YYYY-MM-DD.

    Phase 4 will call this with the most recent quarter to build
    the universe-wide snapshot for index construction.
    """
    _validate_exchange_or_400(exchange, config)
    with db_engine.connect() as conn:
        rows = QuarterlyFinancialsRepository(conn).list_latest_for_exchange(
            exchange,
        )
    return FinancialListPayload(
        exchange=exchange,
        rows=[_finrow_payload(r) for r in rows],
    )


@router.get("/financials/{ticker_id}/quarters",
            response_model=FinancialQuartersPayload)
def get_latest_n_quarters(
    ticker_id: int,
    n: int = Query(4, ge=1, le=20),
    db_engine: Engine = Depends(get_db_engine),
) -> FinancialQuartersPayload:
    """The last N quarters for one ticker, newest first. This is the
    Phase 7 'show Q-4 .. Q-1' query."""
    with db_engine.connect() as conn:
        quarters = QuarterlyFinancialsRepository(conn).get_latest_n_quarters(
            ticker_id, n=n,
        )
    return FinancialQuartersPayload(
        ticker_id=ticker_id,
        rows=[_finrow_payload(q) for q in quarters],
    )


@router.get("/financials/{ticker_id}/quarter/{fiscal_quarter}/history",
            response_model=FinancialHistoryPayload)
def get_quarter_history(
    ticker_id: int,
    fiscal_quarter: "date",
    db_engine: Engine = Depends(get_db_engine),
) -> FinancialHistoryPayload:
    """All snapshots for one (ticker, quarter) pair — used for
    restatement detection. Newest fetched_at first."""
    with db_engine.connect() as conn:
        history = QuarterlyFinancialsRepository(conn).get_history(
            ticker_id, fiscal_quarter,
        )
    return FinancialHistoryPayload(
        ticker_id=ticker_id,
        fiscal_quarter=fiscal_quarter,
        rows=[_finrow_payload(r) for r in history],
    )


@router.post("/refresh-financials",
             response_model=RefreshAcceptedPayload, status_code=202)
def refresh_financials(
    exchange: str = Query(..., min_length=2, max_length=10),
    force: bool = Query(False),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> RefreshAcceptedPayload:
    """Fire-and-forget financials refresh. Returns 202 immediately;
    poll /runs for the result."""
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: refresh-financials requested",
        extra={"exchange": exchange, "force": force},
    )
    executor.submit(_run_financials_refresh_safe, db_engine, config, exchange, force)
    return RefreshAcceptedPayload(
        action="refresh-financials",
        run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes=(
            f"Financials refresh ({exchange}, force={force}) submitted; "
            f"check /v1/fundamentals/runs for progress."
        ),
    )


# ============================================================
# Phase 4A — daily market cap endpoints
# ============================================================


from ....persistence.fundamentals_repo import DailyMarketCapRepository


class MarketCapRowPayload(BaseModel):
    ticker_id: int
    trade_date: date
    close_price: float
    shares_used: Optional[int] = None
    market_cap: float
    source: str
    computed_at: datetime


class MarketCapListPayload(BaseModel):
    trade_date: date
    exchange: Optional[str] = None
    rows: list[MarketCapRowPayload]


class MarketCapHistoryPayload(BaseModel):
    ticker_id: int
    rows: list[MarketCapRowPayload]


def _mcap_row_payload(r) -> MarketCapRowPayload:
    return MarketCapRowPayload(
        ticker_id=r.ticker_id,
        trade_date=r.trade_date,
        close_price=r.close_price,
        shares_used=r.shares_used,
        market_cap=r.market_cap,
        source=r.source,
        computed_at=r.computed_at,
    )


def _run_mcap_backfill_safe(
    db_engine: Engine, config: Config, exchange: str,
    from_date: date, to_date: date, force: bool,
) -> None:
    """Executor wrapper — catches exceptions so the executor thread
    doesn't die silently."""
    from ....fundamentals.runners import run_market_cap_backfill
    try:
        run_market_cap_backfill(
            db_engine, exchange,
            from_date=from_date, to_date=to_date, force=force,
        )
    except Exception:
        logger.exception(
            "Market cap backfill failed for exchange=%s", exchange,
        )


@router.get("/market-cap", response_model=MarketCapListPayload)
def list_market_cap(
    trade_date: date = Query(...),
    exchange: Optional[str] = Query(None, min_length=2, max_length=10),
    limit: int = Query(100, ge=1, le=5000),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> MarketCapListPayload:
    """All market cap rows for `trade_date`, sorted by market_cap DESC.
    Optionally scoped to one exchange. Returns up to `limit` rows.

    This is the read endpoint Phase 4B uses for "top-N tickers by
    market cap on rebalance date X".
    """
    if exchange is not None:
        _validate_exchange_or_400(exchange, config)
    with db_engine.connect() as conn:
        rows = DailyMarketCapRepository(conn).list_for_date(
            trade_date, exchange=exchange, limit=limit,
        )
    return MarketCapListPayload(
        trade_date=trade_date,
        exchange=exchange,
        rows=[_mcap_row_payload(r) for r in rows],
    )


@router.get("/market-cap/{ticker_id}/history",
            response_model=MarketCapHistoryPayload)
def get_market_cap_history(
    ticker_id: int,
    limit: int = Query(365, ge=1, le=3650),
    db_engine: Engine = Depends(get_db_engine),
) -> MarketCapHistoryPayload:
    """Daily market cap history for one ticker, newest first."""
    with db_engine.connect() as conn:
        history = DailyMarketCapRepository(conn).list_history(
            ticker_id, limit=limit,
        )
    return MarketCapHistoryPayload(
        ticker_id=ticker_id,
        rows=[_mcap_row_payload(r) for r in history],
    )


@router.post("/compute-market-cap",
             response_model=RefreshAcceptedPayload, status_code=202)
def compute_market_cap_endpoint(
    exchange: str = Query(..., min_length=2, max_length=10),
    backfill_from: Optional[date] = Query(None),
    backfill_to: Optional[date] = Query(None),
    force: bool = Query(False),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> RefreshAcceptedPayload:
    """Fire-and-forget market cap backfill. Requires backfill_from
    (and optionally backfill_to, defaulting to today). Returns 202
    immediately; poll /runs for the result.

    Single-day compute via this endpoint is not supported — for
    that, use the CLI's `compute-market-cap --date YYYY-MM-DD`
    locally. The HTTP path is for the heavy backfill that benefits
    from the executor.
    """
    from datetime import date as _date
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    if backfill_from is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="backfill_from is required",
        )
    if backfill_to is None:
        backfill_to = _date.today()
    if backfill_from > backfill_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"backfill_from {backfill_from} must be <= "
                   f"backfill_to {backfill_to}",
        )
    logger.info(
        "Admin: compute-market-cap requested",
        extra={"exchange": exchange,
               "backfill_from": str(backfill_from),
               "backfill_to": str(backfill_to),
               "force": force},
    )
    executor.submit(
        _run_mcap_backfill_safe, db_engine, config, exchange,
        backfill_from, backfill_to, force,
    )
    return RefreshAcceptedPayload(
        action="compute-market-cap",
        run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes=(
            f"Market cap backfill ({exchange}, {backfill_from}→"
            f"{backfill_to}, force={force}) submitted; check "
            f"/v1/fundamentals/runs for progress."
        ),
    )
