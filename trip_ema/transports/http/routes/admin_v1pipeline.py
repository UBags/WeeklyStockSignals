"""V1 clean-pipeline admin routes.

Endpoints for the three-runner pipeline (Steps 4–8 of the clean-
pipeline V1 refactor):

  POST /v1/admin/v1pipeline/stock-indicator-state
      ?ticker_id=X&timeframe=1W      — single ticker
      ?exchange=NSE&timeframe=1W     — all active tickers on exchange

  (Step 5 adds the stock-indicator-enrichment endpoint here.)
  (Steps 7–8 add the industry equivalents here.)

Each "fire" endpoint:
  1. Submits the runner function to AppState.etl_executor.
  2. Returns 202 Accepted immediately.
  3. The fundamentals_run table records progress; poll
     `/v1/admin/fundamentals/status` for state.

Mirrors the `admin_etl.py` async-action pattern. Reuses the shared
ETL executor (max_workers=2 by default) — V1 stock state compute is
fast enough (~ms per ticker) that this isn't a contention point.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, UTC

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Engine

from ....config import Config
from ....etl.exchanges import (
    UnsupportedExchangeError,
    validate_exchange,
)
from ....fundamentals.v1_runners import (
    populate_industry_index_value_for_all,
    populate_industry_index_value_for_industry,
    populate_industry_indicator_state_for_all,
    populate_industry_indicator_state_for_industry,
    populate_stock_indicator_enrichment_for_exchange,
    populate_stock_indicator_enrichment_for_ticker,
    populate_stock_indicator_state_for_exchange,
    populate_stock_indicator_state_for_ticker,
)
from ..dependencies import (
    get_config,
    get_db_engine,
    get_etl_executor,
    require_admin_token,
)
from ..schemas import EtlActionAcceptedPayload

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/admin/v1pipeline",
    tags=["admin", "v1pipeline"],
    dependencies=[Depends(require_admin_token)],
)


def _require_executor(executor):
    if executor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "V1 pipeline admin endpoints are disabled. Ensure "
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
        ) from e


# ============================================================
# Stock indicator state populator (Step 4)
# ============================================================


def _run_stock_state_for_ticker_safe(
    db_engine: Engine, ticker_id: int, timeframe: str,
) -> None:
    """Wrap the per-ticker runner so a failure on the executor thread
    is logged rather than swallowed silently."""
    try:
        populate_stock_indicator_state_for_ticker(
            db_engine, ticker_id, timeframe,
        )
    except Exception:
        logger.exception(
            "populate_stock_indicator_state_for_ticker failed: "
            "ticker_id=%d tf=%s",
            ticker_id, timeframe,
        )


def _run_stock_state_for_exchange_safe(
    db_engine: Engine, exchange: str, timeframe: str,
) -> None:
    """Wrap the per-exchange runner the same way."""
    try:
        populate_stock_indicator_state_for_exchange(
            db_engine, exchange, timeframe,
        )
    except Exception:
        logger.exception(
            "populate_stock_indicator_state_for_exchange failed: "
            "exchange=%s tf=%s",
            exchange, timeframe,
        )


@router.post(
    "/stock-indicator-state",
    response_model=EtlActionAcceptedPayload,
)
def admin_v1pipeline_stock_indicator_state(
    timeframe: str = Query("1W", min_length=2, max_length=8),
    ticker_id: int | None = Query(
        None, ge=1,
        description="Compute for a single ticker. Mutually exclusive "
                    "with `exchange`.",
    ),
    exchange: str | None = Query(
        None, min_length=2, max_length=10,
        description="Compute for all active tickers on this exchange. "
                    "Mutually exclusive with `ticker_id`.",
    ),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor=Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger V1 stock indicator state population.

    Provide exactly ONE of `ticker_id` or `exchange`:
      - `ticker_id` — single-ticker compute. Fast (~ms).
      - `exchange` — bulk compute over every active ticker on the
        exchange. Per-ticker error isolation; failures are recorded in
        the run report but don't abort the bulk run.

    Returns 202 Accepted with no run_id (the runner opens its own
    fundamentals_run row on the worker thread; poll the fundamentals
    status endpoint to see it).
    """
    _require_executor(executor)

    # Exactly one of {ticker_id, exchange} must be provided.
    if (ticker_id is None) == (exchange is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Exactly one of `ticker_id` or `exchange` must be "
                "provided (got "
                f"ticker_id={ticker_id}, exchange={exchange})."
            ),
        )

    if ticker_id is not None:
        logger.info(
            "Admin: V1 stock-indicator-state requested for ticker_id=%d "
            "tf=%s",
            ticker_id, timeframe,
        )
        executor.submit(
            _run_stock_state_for_ticker_safe,
            db_engine, ticker_id, timeframe,
        )
        return EtlActionAcceptedPayload(
            action="v1pipeline.stock-indicator-state",
            run_id=None,
            accepted_at=datetime.now(UTC),
            notes=(
                f"V1 stock indicator state submitted for "
                f"ticker_id={ticker_id} tf={timeframe}."
            ),
        )

    # exchange branch
    assert exchange is not None
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: V1 stock-indicator-state requested for "
        "exchange=%s tf=%s (bulk)",
        exchange, timeframe,
    )
    executor.submit(
        _run_stock_state_for_exchange_safe,
        db_engine, exchange, timeframe,
    )
    return EtlActionAcceptedPayload(
        action="v1pipeline.stock-indicator-state",
        run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"V1 stock indicator state submitted for "
            f"exchange={exchange} tf={timeframe} (bulk; per-ticker "
            f"error isolation)."
        ),
    )


# ============================================================
# Stock indicator enrichment populator (Step 5)
# ============================================================


def _run_stock_enrichment_for_ticker_safe(
    db_engine: Engine, ticker_id: int, timeframe: str,
) -> None:
    """Wrap the per-ticker enrichment runner so a failure on the
    executor thread is logged rather than swallowed silently."""
    try:
        populate_stock_indicator_enrichment_for_ticker(
            db_engine, ticker_id, timeframe,
        )
    except Exception:
        logger.exception(
            "populate_stock_indicator_enrichment_for_ticker failed: "
            "ticker_id=%d tf=%s",
            ticker_id, timeframe,
        )


def _run_stock_enrichment_for_exchange_safe(
    db_engine: Engine, exchange: str, timeframe: str,
) -> None:
    """Wrap the per-exchange enrichment runner the same way."""
    try:
        populate_stock_indicator_enrichment_for_exchange(
            db_engine, exchange, timeframe,
        )
    except Exception:
        logger.exception(
            "populate_stock_indicator_enrichment_for_exchange failed: "
            "exchange=%s tf=%s",
            exchange, timeframe,
        )


@router.post(
    "/stock-indicator-enrichment",
    response_model=EtlActionAcceptedPayload,
)
def admin_v1pipeline_stock_indicator_enrichment(
    timeframe: str = Query("1W", min_length=2, max_length=8),
    ticker_id: int | None = Query(
        None, ge=1,
        description="Compute for a single ticker. Mutually exclusive "
                    "with `exchange`.",
    ),
    exchange: str | None = Query(
        None, min_length=2, max_length=10,
        description="Compute for all active tickers on this exchange. "
                    "Mutually exclusive with `ticker_id`.",
    ),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor=Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger V1 stock indicator enrichment population.

    Provide exactly ONE of `ticker_id` or `exchange`. The endpoint
    requires the matching state row(s) to already exist (call the
    Step 4 endpoint first). Tickers with no state are reported as
    skipped, not failed, in the bulk variant.

    Returns 202 Accepted with no run_id.
    """
    _require_executor(executor)

    if (ticker_id is None) == (exchange is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Exactly one of `ticker_id` or `exchange` must be "
                "provided (got "
                f"ticker_id={ticker_id}, exchange={exchange})."
            ),
        )

    if ticker_id is not None:
        logger.info(
            "Admin: V1 stock-indicator-enrichment requested for "
            "ticker_id=%d tf=%s",
            ticker_id, timeframe,
        )
        executor.submit(
            _run_stock_enrichment_for_ticker_safe,
            db_engine, ticker_id, timeframe,
        )
        return EtlActionAcceptedPayload(
            action="v1pipeline.stock-indicator-enrichment",
            run_id=None,
            accepted_at=datetime.now(UTC),
            notes=(
                f"V1 stock indicator enrichment submitted for "
                f"ticker_id={ticker_id} tf={timeframe}."
            ),
        )

    # exchange branch
    assert exchange is not None
    _validate_exchange_or_400(exchange, config)
    logger.info(
        "Admin: V1 stock-indicator-enrichment requested for "
        "exchange=%s tf=%s (bulk)",
        exchange, timeframe,
    )
    executor.submit(
        _run_stock_enrichment_for_exchange_safe,
        db_engine, exchange, timeframe,
    )
    return EtlActionAcceptedPayload(
        action="v1pipeline.stock-indicator-enrichment",
        run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"V1 stock indicator enrichment submitted for "
            f"exchange={exchange} tf={timeframe} (bulk; per-ticker "
            f"error isolation)."
        ),
    )


# ============================================================
# Industry index value populator (Step 6)
# ============================================================


def _run_industry_index_value_for_industry_safe(
    db_engine: Engine,
    industry_name: str,
    from_date: date | None,
    to_date: date | None,
    resume_run_id: int | None,
) -> None:
    """Wrap the per-industry runner so a failure on the executor
    thread is logged rather than swallowed silently."""
    try:
        populate_industry_index_value_for_industry(
            db_engine, industry_name,
            from_date=from_date, to_date=to_date,
            resume_run_id=resume_run_id,
        )
    except Exception:
        logger.exception(
            "populate_industry_index_value_for_industry failed: "
            "industry=%s", industry_name,
        )


def _run_industry_index_value_for_all_safe(
    db_engine: Engine,
    from_date: date | None,
    to_date: date | None,
) -> None:
    """Wrap the bulk runner the same way."""
    try:
        populate_industry_index_value_for_all(
            db_engine, from_date=from_date, to_date=to_date,
        )
    except Exception:
        logger.exception(
            "populate_industry_index_value_for_all failed",
        )


@router.post(
    "/industry-index-value",
    response_model=EtlActionAcceptedPayload,
)
def admin_v1pipeline_industry_index_value(
    industry: str | None = Query(
        None, min_length=1, max_length=200,
        description="Compute for one industry. Mutually exclusive "
                    "with `all_industries`.",
    ),
    all_industries: bool = Query(
        False,
        description="Compute for every ACTIVE industry. Mutually "
                    "exclusive with `industry`.",
    ),
    from_date: date | None = Query(
        None,
        description="Start of date range (inclusive). Default: the "
                    "industry's base_date.",
    ),
    to_date: date | None = Query(
        None,
        description="End of date range (inclusive). Default: today.",
    ),
    resume_run_id: int | None = Query(
        None, ge=1,
        description="Resume an existing RUNNING run by id. Only "
                    "valid with single `industry`.",
    ),
    db_engine: Engine = Depends(get_db_engine),
    executor=Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger V1 industry index value population.

    Provide exactly ONE of `industry` or `all_industries=true`. The
    per-industry variant is resumable via `resume_run_id`; the bulk
    variant is not (but is upsert-idempotent — safe to re-run).

    The runner walks every trading day in the range (defaulted from
    industry.base_date to today) and upserts the computed index
    value. No skip-if-recent guard; every invocation is a full
    recompute of the requested range.

    Returns 202 Accepted with no run_id (the runner opens its own
    fundamentals_run row on the worker thread).
    """
    _require_executor(executor)

    if (industry is None) == (not all_industries):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Exactly one of `industry` or "
                "`all_industries=true` must be provided (got "
                f"industry={industry}, "
                f"all_industries={all_industries})."
            ),
        )

    if resume_run_id is not None and all_industries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "`resume_run_id` is only valid with a single "
                "`industry`. The bulk variant does not support "
                "resume; re-run it (upsert-idempotent) or resume "
                "individual industries."
            ),
        )

    if (from_date is not None and to_date is not None
            and from_date > to_date):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"from_date {from_date} must be <= to_date {to_date}"
            ),
        )

    if industry is not None:
        logger.info(
            "Admin: V1 industry-index-value requested for "
            "industry=%s (from=%s to=%s resume=%s)",
            industry, from_date, to_date, resume_run_id,
        )
        executor.submit(
            _run_industry_index_value_for_industry_safe,
            db_engine, industry, from_date, to_date, resume_run_id,
        )
        notes_parts = [
            f"V1 industry index value submitted for industry={industry}"
        ]
        if from_date is not None:
            notes_parts.append(f"from_date={from_date}")
        if to_date is not None:
            notes_parts.append(f"to_date={to_date}")
        if resume_run_id is not None:
            notes_parts.append(f"resume_run_id={resume_run_id}")
        return EtlActionAcceptedPayload(
            action="v1pipeline.industry-index-value",
            run_id=None,
            accepted_at=datetime.now(UTC),
            notes=" ".join(notes_parts) + ".",
        )

    # all_industries=True branch
    logger.info(
        "Admin: V1 industry-index-value requested for ALL active "
        "industries (from=%s to=%s)",
        from_date, to_date,
    )
    executor.submit(
        _run_industry_index_value_for_all_safe,
        db_engine, from_date, to_date,
    )
    return EtlActionAcceptedPayload(
        action="v1pipeline.industry-index-value",
        run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"V1 industry index value submitted for all active "
            f"industries (from_date={from_date}, to_date={to_date}; "
            f"bulk; per-industry error isolation)."
        ),
    )


# ============================================================
# Industry indicator state populator (Step 7)
# ============================================================


def _run_industry_indicator_state_for_industry_safe(
    db_engine: Engine,
    industry_name: str,
    timeframe: str,
    min_bars: int,
) -> None:
    """Wrap the per-industry runner so a failure on the executor
    thread is logged rather than swallowed silently."""
    try:
        populate_industry_indicator_state_for_industry(
            db_engine, industry_name,
            timeframe=timeframe, min_bars=min_bars,
        )
    except Exception:
        logger.exception(
            "populate_industry_indicator_state_for_industry "
            "failed: industry=%s tf=%s", industry_name, timeframe,
        )


def _run_industry_indicator_state_for_all_safe(
    db_engine: Engine,
    timeframe: str,
    min_bars: int,
) -> None:
    """Wrap the bulk runner the same way."""
    try:
        populate_industry_indicator_state_for_all(
            db_engine, timeframe=timeframe, min_bars=min_bars,
        )
    except Exception:
        logger.exception(
            "populate_industry_indicator_state_for_all failed: "
            "tf=%s", timeframe,
        )


@router.post(
    "/industry-indicator-state",
    response_model=EtlActionAcceptedPayload,
)
def admin_v1pipeline_industry_indicator_state(
    industry: str | None = Query(
        None, min_length=1, max_length=200,
        description="Compute for one industry. Mutually exclusive "
                    "with `all_industries`.",
    ),
    all_industries: bool = Query(
        False,
        description="Compute for every ACTIVE industry. Mutually "
                    "exclusive with `industry`.",
    ),
    timeframe: str = Query(
        "Weekly",
        description="Always 'Weekly' in V1. Column kept for future "
                    "multi-timeframe support.",
    ),
    min_bars: int = Query(
        30, ge=1, le=1000,
        description="Minimum weekly bars required to seed EMAs. "
                    "Default 30.",
    ),
    db_engine: Engine = Depends(get_db_engine),
    executor=Depends(get_etl_executor),
) -> EtlActionAcceptedPayload:
    """Trigger V1 industry indicator state population.

    Provide exactly ONE of `industry` or `all_industries=true`.
    Reads industry_index_value (populated by Step 6) and computes
    the 12-core indicator state on IST-Monday-anchored weekly bars.
    True-range ATR (synthetic_atr_for_index=False).

    Returns 202 Accepted with no run_id (the runner opens its own
    fundamentals_run row on the worker thread).
    """
    _require_executor(executor)

    if (industry is None) == (not all_industries):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Exactly one of `industry` or "
                "`all_industries=true` must be provided (got "
                f"industry={industry}, "
                f"all_industries={all_industries})."
            ),
        )

    if industry is not None:
        logger.info(
            "Admin: V1 industry-indicator-state requested for "
            "industry=%s tf=%s min_bars=%d",
            industry, timeframe, min_bars,
        )
        executor.submit(
            _run_industry_indicator_state_for_industry_safe,
            db_engine, industry, timeframe, min_bars,
        )
        return EtlActionAcceptedPayload(
            action="v1pipeline.industry-indicator-state",
            run_id=None,
            accepted_at=datetime.now(UTC),
            notes=(
                f"V1 industry indicator state submitted for "
                f"industry={industry} tf={timeframe}."
            ),
        )

    # all_industries=True
    logger.info(
        "Admin: V1 industry-indicator-state requested for ALL "
        "active industries tf=%s min_bars=%d",
        timeframe, min_bars,
    )
    executor.submit(
        _run_industry_indicator_state_for_all_safe,
        db_engine, timeframe, min_bars,
    )
    return EtlActionAcceptedPayload(
        action="v1pipeline.industry-indicator-state",
        run_id=None,
        accepted_at=datetime.now(UTC),
        notes=(
            f"V1 industry indicator state submitted for all active "
            f"industries tf={timeframe} (bulk; per-industry error "
            f"isolation)."
        ),
    )
