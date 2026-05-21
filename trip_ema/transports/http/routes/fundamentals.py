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
from datetime import datetime, timezone
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
