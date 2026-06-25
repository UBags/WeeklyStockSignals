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
from datetime import date, datetime, UTC

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
    sector: str | None = None
    industry: str | None = None
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
    exchange: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    status: str
    succeeded: int
    failed: int
    skipped: int


class RunListPayload(BaseModel):
    rows: list[RunRowPayload]


class RefreshAcceptedPayload(BaseModel):
    action: str
    run_id: int | None = None
    accepted_at: datetime
    notes: str


class OverrideRequestPayload(BaseModel):
    ticker_id: int
    sector: str = "Unclassified"
    industry: str = Field(..., min_length=1, max_length=128)
    note: str | None = None
    set_by: str | None = None


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
    phase: str | None = Query(None),
    exchange: str | None = Query(None, min_length=2, max_length=10),
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
        accepted_at=datetime.now(UTC),
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
    as_of_date: date
    shares_outstanding: int | None = None
    free_float_factor: float | None = None
    shares_source: str
    fetched_at: datetime
    xbrl_url: str | None = None


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
        accepted_at=datetime.now(UTC),
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
    fiscal_quarter: date
    fetched_at: datetime
    revenue: float | None = None
    ebitda: float | None = None
    currency: str
    source: str


class FinancialListPayload(BaseModel):
    exchange: str
    rows: list[FinancialRowPayload]


class FinancialHistoryPayload(BaseModel):
    ticker_id: int
    fiscal_quarter: date
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
    fiscal_quarter: date,
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
        accepted_at=datetime.now(UTC),
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
    shares_used: int | None = None
    market_cap: float
    source: str
    computed_at: datetime


class MarketCapListPayload(BaseModel):
    trade_date: date
    exchange: str | None = None
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
    exchange: str | None = Query(None, min_length=2, max_length=10),
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
    backfill_from: date | None = Query(None),
    backfill_to: date | None = Query(None),
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
        accepted_at=datetime.now(UTC),
        notes=(
            f"Market cap backfill ({exchange}, {backfill_from}→"
            f"{backfill_to}, force={force}) submitted; check "
            f"/v1/fundamentals/runs for progress."
        ),
    )


# ============================================================
# Phase 4B-3 — industry index endpoints
# ============================================================


from ....persistence.fundamentals_repo import (
    IndustryIndexConstituentRepository,
    IndustryIndexRepository,
    IndustryIndexValueRepository,
)


# ---------- payload models ---------- #


class IndustryIndexPayload(BaseModel):
    industry_name: str
    sector_name: str
    methodology: str
    base_value: float
    base_date: date
    rebalance_schedule: str
    min_constituents: int
    status: str
    notes: str | None = None


class IndustryIndexListPayload(BaseModel):
    rows: list[IndustryIndexPayload]


class IndustryIndexConstituentPayload(BaseModel):
    industry_name: str
    ticker_id: int
    rebalance_date: date
    effective_from: date
    effective_to: date | None = None
    weight_at_rebalance: float
    market_cap_at_rebalance: float
    close_at_rebalance: float
    removed_reason: str | None = None


class IndustryIndexConstituentListPayload(BaseModel):
    industry_name: str
    as_of: date | None = None
    rows: list[IndustryIndexConstituentPayload]


class IndustryIndexValuePayload(BaseModel):
    industry_name: str
    trade_date: date
    value: float
    constituents_used: int
    constituents_missing: int
    renormalized: bool
    effective_rebalance_date: date
    computed_at: datetime


class IndustryIndexValueListPayload(BaseModel):
    industry_name: str
    from_date: date | None = None
    to_date: date | None = None
    rows: list[IndustryIndexValuePayload]


def _index_payload(idx) -> IndustryIndexPayload:
    return IndustryIndexPayload(
        industry_name=idx.industry_name,
        sector_name=idx.sector_name,
        methodology=idx.methodology,
        base_value=idx.base_value,
        base_date=idx.base_date,
        rebalance_schedule=idx.rebalance_schedule,
        min_constituents=idx.min_constituents,
        status=idx.status,
        notes=idx.notes,
    )


def _constituent_payload(c) -> IndustryIndexConstituentPayload:
    return IndustryIndexConstituentPayload(
        industry_name=c.industry_name,
        ticker_id=c.ticker_id,
        rebalance_date=c.rebalance_date,
        effective_from=c.effective_from,
        effective_to=c.effective_to,
        weight_at_rebalance=c.weight_at_rebalance,
        market_cap_at_rebalance=c.market_cap_at_rebalance,
        close_at_rebalance=c.close_at_rebalance,
        removed_reason=c.removed_reason,
    )


def _value_payload(v) -> IndustryIndexValuePayload:
    return IndustryIndexValuePayload(
        industry_name=v.industry_name,
        trade_date=v.trade_date,
        value=v.value,
        constituents_used=v.constituents_used,
        constituents_missing=v.constituents_missing,
        renormalized=v.renormalized,
        effective_rebalance_date=v.effective_rebalance_date,
        computed_at=v.computed_at,
    )


# ---------- executor wrappers (for fire-and-forget endpoints) ---------- #


def _run_industry_index_build_safe(
    db_engine: Engine, config: Config, exchange: str,
    min_constituents: int, n: int, base_value: float,
) -> None:
    from ....fundamentals.runners import run_industry_index_build
    try:
        run_industry_index_build(
            db_engine, exchange,
            min_constituents=min_constituents,
            n=n,
            base_value=base_value,
        )
    except Exception:
        logger.exception(
            "Industry index build failed for exchange=%s", exchange,
        )


def _run_index_value_backfill_safe(
    db_engine: Engine, industry_name: str,
    from_date_param: date | None, to_date_param: date | None,
    force: bool, all_active: bool,
) -> None:
    """Runs either one industry or all active industries."""
    from ....fundamentals.runners import run_index_value_backfill
    try:
        with db_engine.connect() as conn:
            ir = IndustryIndexRepository(conn)
            if all_active:
                indices = ir.list_active()
            else:
                idx = ir.get(industry_name)
                indices = [idx] if idx else []

        effective_to = to_date_param or date.today()
        for idx in indices:
            effective_from = (
                max(from_date_param, idx.base_date)
                if from_date_param else idx.base_date
            )
            if effective_from > effective_to:
                continue
            run_index_value_backfill(
                db_engine, idx.industry_name,
                from_date=effective_from,
                to_date=effective_to,
                force=force,
            )
    except Exception:
        logger.exception(
            "Index value backfill failed for industry=%s "
            "all_active=%s", industry_name, all_active,
        )


# ---------- GET endpoints ---------- #


@router.get("/industry-indices", response_model=IndustryIndexListPayload)
def list_industry_indices(
    include_retired: bool = Query(False),
    db_engine: Engine = Depends(get_db_engine),
) -> IndustryIndexListPayload:
    """List all industry indices. By default returns only ACTIVE
    ones; pass include_retired=true to include RETIRED."""
    with db_engine.connect() as conn:
        ir = IndustryIndexRepository(conn)
        if include_retired:
            indices = ir.list_all()
        else:
            indices = ir.list_active()
    return IndustryIndexListPayload(
        rows=[_index_payload(i) for i in indices],
    )


@router.get("/industry-indices/{industry_name:path}/constituents",
            response_model=IndustryIndexConstituentListPayload)
def get_industry_index_constituents(
    industry_name: str,
    as_of: date | None = Query(None),
    db_engine: Engine = Depends(get_db_engine),
) -> IndustryIndexConstituentListPayload:
    """List constituents of an index. Without as_of, returns the
    currently-active constituents (effective_to IS NULL). With
    as_of=YYYY-MM-DD, returns constituents whose effective period
    contains that date."""
    with db_engine.connect() as conn:
        cr = IndustryIndexConstituentRepository(conn)
        if as_of is not None:
            rows = cr.list_as_of(industry_name, as_of)
        else:
            rows = cr.list_active_for_index(industry_name)
    return IndustryIndexConstituentListPayload(
        industry_name=industry_name,
        as_of=as_of,
        rows=[_constituent_payload(c) for c in rows],
    )


@router.get("/industry-indices/{industry_name:path}/values",
            response_model=IndustryIndexValueListPayload)
def get_industry_index_values(
    industry_name: str,
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    limit: int | None = Query(None, ge=1, le=5000),
    db_engine: Engine = Depends(get_db_engine),
) -> IndustryIndexValueListPayload:
    """Get the daily value time series for an index. Returns
    newest-first; pass from_date/to_date to restrict the range,
    and limit to cap the row count."""
    with db_engine.connect() as conn:
        history = IndustryIndexValueRepository(conn).get_history(
            industry_name,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
    return IndustryIndexValueListPayload(
        industry_name=industry_name,
        from_date=from_date,
        to_date=to_date,
        rows=[_value_payload(v) for v in history],
    )


# Note: GET /industry-indices/{name} for the index definition is
# served by /industry-indices?industry_name=NAME — but FastAPI's
# routing with the {path} subroutes above means a name-with-slash
# (none currently exist in yfinance Indian industries but defensive)
# could conflict. Adding an explicit single-index endpoint:


@router.get("/industry-indices/{industry_name:path}",
            response_model=IndustryIndexPayload)
def get_industry_index(
    industry_name: str,
    db_engine: Engine = Depends(get_db_engine),
) -> IndustryIndexPayload:
    """Get the definition of one index."""
    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(industry_name)
    if idx is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Industry {industry_name!r} is not indexed",
        )
    return _index_payload(idx)


# ---------- POST (fire-and-forget) endpoints ---------- #


@router.post("/build-industry-indices",
             response_model=RefreshAcceptedPayload, status_code=202)
def build_industry_indices_endpoint(
    exchange: str = Query(..., min_length=2, max_length=10),
    min_constituents: int = Query(10, ge=1),
    n: int = Query(10, ge=1),
    base_value: float = Query(1000.0, gt=0),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> RefreshAcceptedPayload:
    """Fire-and-forget auto-discovery + initial rebalance for all
    qualifying industries on the exchange. Returns 202 immediately.
    Idempotent: indices that already exist with constituents are
    left alone."""
    _require_executor(executor)
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: build-industry-indices requested",
        extra={"exchange": exchange,
               "min_constituents": min_constituents,
               "n": n, "base_value": base_value},
    )
    executor.submit(
        _run_industry_index_build_safe, db_engine, config, exchange,
        min_constituents, n, base_value,
    )
    return RefreshAcceptedPayload(
        action="build-industry-indices",
        run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"Industry index build ({exchange}, "
            f"min_constituents={min_constituents}) submitted."
        ),
    )


@router.post("/compute-index-values",
             response_model=RefreshAcceptedPayload, status_code=202)
def compute_index_values_endpoint(
    industry: str | None = Query(None),
    all_active: bool = Query(False),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    force: bool = Query(False),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> RefreshAcceptedPayload:
    """Fire-and-forget index value backfill. Pass either industry=NAME
    or all_active=true. Returns 202 immediately."""
    _require_executor(executor)
    if industry is None and not all_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide either industry=NAME or all_active=true",
        )
    if industry is not None and all_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="industry and all_active are mutually exclusive",
        )
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"from_date {from_date} must be <= to_date {to_date}",
        )
    logger.info(
        "Admin: compute-index-values requested",
        extra={"industry": industry, "all_active": all_active,
               "from_date": str(from_date), "to_date": str(to_date),
               "force": force},
    )
    executor.submit(
        _run_index_value_backfill_safe, db_engine,
        industry or "", from_date, to_date, force, all_active,
    )
    return RefreshAcceptedPayload(
        action="compute-index-values",
        run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"Index value backfill (industry={industry!r}, "
            f"all_active={all_active}) submitted."
        ),
    )


# ============================================================
# Phase 5-4 — indicator state endpoints
# ============================================================


from ....persistence.fundamentals_repo import IndicatorStateRepository


# ---------- payload models ---------- #


class IndicatorStatePayload(BaseModel):
    """One indicator_state row, polymorphic on series identity."""
    ticker_id: int | None = None
    timeframe: str | None = None
    industry_name: str | None = None
    trade_date: date
    med_ema: float
    long_ema: float
    longest_ema: float
    atr: float
    upper_inner_band: float
    lower_inner_band: float
    upper_mid_band: float
    lower_mid_band: float
    upper_outer_band: float
    lower_outer_band: float
    display_regime: int
    pierces_upper_mid: bool
    pierces_lower_mid: bool
    dist_to_med_in_hl_units: float | None = None
    dist_to_long_in_hl_units: float | None = None
    dist_to_longest_in_hl_units: float | None = None
    mean_dist_to_med_in_hl_units: float | None = None
    mean_dist_to_long_in_hl_units: float | None = None
    mean_dist_to_longest_in_hl_units: float | None = None
    bars_since_med_turn: int | None = None
    bars_since_long_turn: int | None = None
    bars_since_longest_turn: int | None = None
    pct_change_since_med_turn: float | None = None
    pct_change_since_long_turn: float | None = None
    pct_change_since_longest_turn: float | None = None
    med_pattern_label: str
    long_pattern_label: str
    longest_pattern_label: str
    med_turn_buy: bool
    med_turn_sell: bool
    long_turn_buy: bool
    long_turn_sell: bool
    longest_turn_buy: bool
    longest_turn_sell: bool
    computed_at: datetime


class IndicatorStateListPayload(BaseModel):
    industry_name: str | None = None
    ticker_id: int | None = None
    from_date: date | None = None
    to_date: date | None = None
    rows: list[IndicatorStatePayload]


def _indicator_state_payload(s) -> IndicatorStatePayload:
    return IndicatorStatePayload(
        ticker_id=s.ticker_id,
        timeframe=s.timeframe,
        industry_name=s.industry_name,
        trade_date=s.trade_date,
        med_ema=s.med_ema,
        long_ema=s.long_ema,
        longest_ema=s.longest_ema,
        atr=s.atr,
        upper_inner_band=s.upper_inner_band,
        lower_inner_band=s.lower_inner_band,
        upper_mid_band=s.upper_mid_band,
        lower_mid_band=s.lower_mid_band,
        upper_outer_band=s.upper_outer_band,
        lower_outer_band=s.lower_outer_band,
        display_regime=s.display_regime,
        pierces_upper_mid=s.pierces_upper_mid,
        pierces_lower_mid=s.pierces_lower_mid,
        dist_to_med_in_hl_units=s.dist_to_med_in_hl_units,
        dist_to_long_in_hl_units=s.dist_to_long_in_hl_units,
        dist_to_longest_in_hl_units=s.dist_to_longest_in_hl_units,
        mean_dist_to_med_in_hl_units=s.mean_dist_to_med_in_hl_units,
        mean_dist_to_long_in_hl_units=s.mean_dist_to_long_in_hl_units,
        mean_dist_to_longest_in_hl_units=s.mean_dist_to_longest_in_hl_units,
        bars_since_med_turn=s.bars_since_med_turn,
        bars_since_long_turn=s.bars_since_long_turn,
        bars_since_longest_turn=s.bars_since_longest_turn,
        pct_change_since_med_turn=s.pct_change_since_med_turn,
        pct_change_since_long_turn=s.pct_change_since_long_turn,
        pct_change_since_longest_turn=s.pct_change_since_longest_turn,
        med_pattern_label=s.med_pattern_label,
        long_pattern_label=s.long_pattern_label,
        longest_pattern_label=s.longest_pattern_label,
        med_turn_buy=s.med_turn_buy,
        med_turn_sell=s.med_turn_sell,
        long_turn_buy=s.long_turn_buy,
        long_turn_sell=s.long_turn_sell,
        longest_turn_buy=s.longest_turn_buy,
        longest_turn_sell=s.longest_turn_sell,
        computed_at=s.computed_at,
    )


# ---------- executor wrapper ---------- #


def _run_indicator_state_safe(
    db_engine: Engine, industry_name: str | None,
    force: bool, all_industries: bool,
    ticker: str | None = None,
    all_tickers: bool = False,
    exchange: str | None = None,
    timeframe: str | None = None,
) -> None:
    """Runs the indicator-state compute corresponding to the mode
    requested. Catches exceptions so the executor doesn't die."""
    from ....fundamentals.runners import (
        run_indicator_state_for_all_industries,
        run_indicator_state_for_all_tickers,
        run_indicator_state_for_industry,
        run_indicator_state_for_ticker,
    )
    from ....persistence.tickers_repo import TickersRepository
    from ....domain import TickerRef
    try:
        if all_industries:
            run_indicator_state_for_all_industries(db_engine, force=force)
        elif all_tickers:
            if not timeframe:
                logger.error(
                    "all_tickers mode requires timeframe; aborting "
                    "(timeframe=%r)", timeframe,
                )
                return
            run_indicator_state_for_all_tickers(
                db_engine, timeframe, exchange=exchange, force=force,
            )
        elif ticker:
            if not exchange or not timeframe:
                logger.error(
                    "ticker mode requires exchange and timeframe; aborting "
                    "(exchange=%r timeframe=%r ticker=%r)",
                    exchange, timeframe, ticker,
                )
                return
            ref = TickerRef(exchange, ticker, "EQ")
            with db_engine.connect() as conn:
                t = TickersRepository(conn).get_by_ref(ref)
            if t is None:
                logger.error("Ticker not found: %s:%s:EQ", exchange, ticker)
                return
            run_indicator_state_for_ticker(
                db_engine, t.ticker_id, timeframe, force=force,
            )
        else:
            if not industry_name:
                logger.error(
                    "industry mode requires industry_name; aborting "
                    "(industry_name=%r)", industry_name,
                )
                return
            run_indicator_state_for_industry(
                db_engine, industry_name, force=force,
            )
    except Exception:
        logger.exception(
            "Indicator state compute failed for industry=%s "
            "all_industries=%s ticker=%s all_tickers=%s",
            industry_name, all_industries, ticker, all_tickers,
        )


# ---------- GET endpoints ---------- #


@router.get("/indicator-state/industries/{industry_name:path}",
            response_model=IndicatorStateListPayload)
def get_industry_indicator_state(
    industry_name: str,
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    limit: int | None = Query(None, ge=1, le=5000),
    db_engine: Engine = Depends(get_db_engine),
) -> IndicatorStateListPayload:
    """Get the indicator_state time series for one industry index.

    Returns newest-first; pass from_date/to_date to restrict the
    range, and limit to cap the row count."""
    with db_engine.connect() as conn:
        history = IndicatorStateRepository(conn).get_history_for_industry(
            industry_name,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
    return IndicatorStateListPayload(
        industry_name=industry_name,
        from_date=from_date,
        to_date=to_date,
        rows=[_indicator_state_payload(s) for s in history],
    )


@router.get("/indicator-state/tickers/{ticker_id}",
            response_model=IndicatorStateListPayload)
def get_ticker_indicator_state(
    ticker_id: int,
    timeframe: str = Query(..., min_length=1, max_length=8),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    limit: int | None = Query(None, ge=1, le=5000),
    db_engine: Engine = Depends(get_db_engine),
) -> IndicatorStateListPayload:
    """Get the indicator_state time series for one ticker bar series.

    Phase 5-4 ships the schema and route shape; per-ticker compute
    isn't wired in yet (only per-industry). Empty results expected
    until Phase 6+ adds per-ticker indicator compute."""
    with db_engine.connect() as conn:
        history = IndicatorStateRepository(conn).get_history_for_ticker(
            ticker_id, timeframe,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
    return IndicatorStateListPayload(
        ticker_id=ticker_id,
        from_date=from_date,
        to_date=to_date,
        rows=[_indicator_state_payload(s) for s in history],
    )


# ---------- POST (fire-and-forget) endpoints ---------- #


@router.post("/compute-indicator-state",
             response_model=RefreshAcceptedPayload, status_code=202)
def compute_indicator_state_endpoint(
    industry: str | None = Query(None),
    all_industries: bool = Query(False),
    ticker: str | None = Query(None),
    all_tickers: bool = Query(False),
    exchange: str | None = Query(None),
    timeframe: str | None = Query(None),
    force: bool = Query(False),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> RefreshAcceptedPayload:
    """Fire-and-forget indicator state compute. Modes (mutually
    exclusive): industry, all_industries, ticker, all_tickers."""
    _require_executor(executor)
    modes_set = sum([
        bool(industry), bool(all_industries),
        bool(ticker), bool(all_tickers),
    ])
    if modes_set == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Must provide one of: industry=NAME, "
                "all_industries=true, ticker=SYMBOL, or all_tickers=true"
            ),
        )
    if modes_set > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="industry/all_industries/ticker/all_tickers are mutually exclusive",
        )
    if ticker and (not exchange or not timeframe):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ticker requires both exchange and timeframe",
        )
    if all_tickers and not timeframe:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="all_tickers requires timeframe",
        )
    logger.info(
        "Admin: compute-indicator-state requested",
        extra={"industry": industry, "all_industries": all_industries,
               "ticker": ticker, "all_tickers": all_tickers,
               "exchange": exchange, "timeframe": timeframe,
               "force": force},
    )
    executor.submit(
        _run_indicator_state_safe, db_engine,
        industry, force, all_industries,
        ticker, all_tickers, exchange, timeframe,
    )
    return RefreshAcceptedPayload(
        action="compute-indicator-state",
        run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"Indicator state compute (industry={industry!r}, "
            f"all_industries={all_industries}, ticker={ticker!r}, "
            f"all_tickers={all_tickers}, timeframe={timeframe!r}) "
            f"submitted."
        ),
    )


# ============================================================
# Phase 6-1 — signal event endpoints
# ============================================================


from ....persistence.fundamentals_repo import SignalEventRepository


# ---------- payload models ---------- #


class SignalEventPayload(BaseModel):
    """One signal_event row."""
    ticker_id: int | None = None
    timeframe: str | None = None
    industry_name: str | None = None
    trade_date: date
    event_kind: str
    regime_before: int
    regime_after: int
    close_at_event: float
    computed_at: datetime


class SignalEventListPayload(BaseModel):
    industry_name: str | None = None
    ticker_id: int | None = None
    from_date: date | None = None
    to_date: date | None = None
    kind: str | None = None
    rows: list[SignalEventPayload]


def _signal_event_payload(e) -> SignalEventPayload:
    return SignalEventPayload(
        ticker_id=e.ticker_id,
        timeframe=e.timeframe,
        industry_name=e.industry_name,
        trade_date=e.trade_date,
        event_kind=e.event_kind,
        regime_before=e.regime_before,
        regime_after=e.regime_after,
        close_at_event=e.close_at_event,
        computed_at=e.computed_at,
    )


# ---------- executor wrapper ---------- #


def _run_signals_safe(
    db_engine: Engine,
    force: bool,
    ticker: str | None = None,
    all_tickers: bool = False,
    exchange: str | None = None,
    timeframe: str | None = None,
) -> None:
    from ....fundamentals.runners import (
        run_signal_events_for_all_tickers,
        run_signal_events_for_ticker,
    )
    from ....persistence.tickers_repo import TickersRepository
    from ....domain import TickerRef
    try:
        if all_tickers:
            if not timeframe:
                logger.error(
                    "all_tickers mode requires timeframe; aborting "
                    "(timeframe=%r)", timeframe,
                )
                return
            run_signal_events_for_all_tickers(
                db_engine, timeframe, exchange=exchange, force=force,
            )
        elif ticker:
            if not exchange or not timeframe:
                logger.error(
                    "ticker mode requires exchange and timeframe; aborting "
                    "(exchange=%r timeframe=%r ticker=%r)",
                    exchange, timeframe, ticker,
                )
                return
            ref = TickerRef(exchange, ticker, "EQ")
            with db_engine.connect() as conn:
                t = TickersRepository(conn).get_by_ref(ref)
            if t is None:
                logger.error("Ticker not found: %s:%s:EQ", exchange, ticker)
                return
            run_signal_events_for_ticker(
                db_engine, t.ticker_id, timeframe, force=force,
            )
        else:
            logger.error(
                "Signal events compute called with no mode set "
                "(ticker=%s all_tickers=%s)",
                ticker, all_tickers,
            )
    except Exception:
        logger.exception(
            "Signal events compute failed for "
            "ticker=%s all_tickers=%s",
            ticker, all_tickers,
        )


# ---------- GET endpoints ---------- #


@router.get("/signal-events/recent",
            response_model=SignalEventListPayload)
def list_recent_signal_events(
    kind: str | None = Query(None),
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(100, ge=1, le=5000),
    db_engine: Engine = Depends(get_db_engine),
) -> SignalEventListPayload:
    """Cross-industry scanner: events in the last N days, optionally
    filtered by kind. Newest-first."""
    from ....fundamentals import SIGNAL_EVENT_KINDS
    if kind is not None and kind not in SIGNAL_EVENT_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"kind must be one of {SIGNAL_EVENT_KINDS}, "
                   f"got {kind!r}",
        )
    with db_engine.connect() as conn:
        rows = SignalEventRepository(conn).list_recent(
            kind=kind, days=days, limit=limit,
        )
    return SignalEventListPayload(
        kind=kind,
        rows=[_signal_event_payload(e) for e in rows],
    )


# ---------- POST (fire-and-forget) endpoint ---------- #


@router.post("/compute-signals",
             response_model=RefreshAcceptedPayload, status_code=202)
def compute_signals_endpoint(
    ticker: str | None = Query(None),
    all_tickers: bool = Query(False),
    exchange: str | None = Query(None),
    timeframe: str | None = Query(None),
    force: bool = Query(False),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> RefreshAcceptedPayload:
    """Fire-and-forget signal events compute. Modes (mutually
    exclusive): ticker, all_tickers."""
    _require_executor(executor)
    modes_set = sum([bool(ticker), bool(all_tickers)])
    if modes_set == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide one of: ticker=SYMBOL, or all_tickers=true",
        )
    if modes_set > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ticker/all_tickers are mutually exclusive",
        )
    if ticker and (not exchange or not timeframe):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ticker requires both exchange and timeframe",
        )
    if all_tickers and not timeframe:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="all_tickers requires timeframe",
        )
    logger.info(
        "Admin: compute-signals requested",
        extra={"ticker": ticker, "all_tickers": all_tickers,
               "exchange": exchange, "timeframe": timeframe,
               "force": force},
    )
    executor.submit(
        _run_signals_safe, db_engine, force,
        ticker, all_tickers, exchange, timeframe,
    )
    return RefreshAcceptedPayload(
        action="compute-signals",
        run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"Signal events compute (ticker={ticker!r}, "
            f"all_tickers={all_tickers}, timeframe={timeframe!r}) "
            f"submitted."
        ),
    )


# ============================================================
# Phase 4C — EoD pipeline endpoint
# ============================================================


def _run_eod_pipeline_safe(
    db_engine: Engine, exchange: str,
    include_tickers: bool, timeframe: str, force: bool,
) -> None:
    """Executor wrapper: catches exceptions so the pipeline failing
    doesn't kill the executor thread."""
    from ....fundamentals.eod_pipeline import run_eod_pipeline
    try:
        result = run_eod_pipeline(
            db_engine, exchange,
            include_tickers=include_tickers,
            timeframe=timeframe, force=force,
        )
        logger.info(
            "EoD pipeline finished via HTTP: exchange=%s status=%s "
            "duration=%.1fs",
            exchange, result.overall_status,
            result.duration_seconds or 0.0,
        )
    except Exception:
        logger.exception(
            "EoD pipeline raised via HTTP for exchange=%s", exchange,
        )


@router.post("/run-eod-pipeline",
             response_model=RefreshAcceptedPayload, status_code=202)
def run_eod_pipeline_endpoint(
    exchange: str = Query(..., min_length=1, max_length=8),
    include_tickers: bool = Query(False),
    timeframe: str = Query("1W", min_length=1, max_length=8),
    force: bool = Query(False),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> RefreshAcceptedPayload:
    """Fire-and-forget EoD pipeline. Runs 8 phases for one exchange
    (10 with include_tickers=True)."""
    _require_executor(executor)
    logger.info(
        "Admin: run-eod-pipeline requested",
        extra={"exchange": exchange,
               "include_tickers": include_tickers,
               "timeframe": timeframe, "force": force},
    )
    executor.submit(
        _run_eod_pipeline_safe, db_engine,
        exchange, include_tickers, timeframe, force,
    )
    return RefreshAcceptedPayload(
        action="run-eod-pipeline",
        run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"EoD pipeline (exchange={exchange}, "
            f"include_tickers={include_tickers}, "
            f"timeframe={timeframe}, force={force}) submitted."
        ),
    )
