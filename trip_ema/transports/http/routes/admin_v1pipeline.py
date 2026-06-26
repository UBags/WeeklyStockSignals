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
from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Engine

from ....config import Config
from ....etl.exchanges import (
    UnsupportedExchangeError,
    validate_exchange,
)
from ....fundamentals.v1_runners import (
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
