"""Stateless compute — the (a) path.

POST /v1/compute
    Body: ComputeRequest (ticker + history + latest_bar + timeframe + verbose)
    → TerseResponsePayload or VerboseResponsePayload

Builds a throwaway TickerEngine, primes with the supplied history,
ingests the latest bar, returns the result. Does NOT touch the cache,
DB, or registry. Use this for ad-hoc analysis or for tickers not in the
warm set.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ....api import compute_full_terse, compute_full_verbose
from ....config import Config
from ..dependencies import get_config
from ..schemas import (
    ComputeRequest,
    TerseResponsePayload,
    VerboseResponsePayload,
)

router = APIRouter(prefix="/v1", tags=["compute"])


@router.post(
    "/compute",
    response_model=TerseResponsePayload | VerboseResponsePayload,
)
def compute(
    request: ComputeRequest,
    config: Config = Depends(get_config),
):
    """Run a stateless compute. Uses indicator params from the loaded
    config — callers can't override these via the API, by design."""
    params = config.to_indicator_params()
    ref = request.ticker_ref()
    try:
        history = [b.to_domain() for b in request.history]
        latest = request.latest_bar.to_domain()
    except ValueError as e:
        # OHLCVBar.__post_init__ raises on naive timestamps
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        ) from e

    if request.verbose:
        result = compute_full_verbose(
            ticker=ref, history=history, latest_bar=latest,
            timeframe=request.timeframe, params=params,
        )
        return VerboseResponsePayload.from_domain(result)
    result_terse = compute_full_terse(
        ticker=ref, history=history, latest_bar=latest,
        timeframe=request.timeframe, params=params,
    )
    return TerseResponsePayload.from_domain(result_terse)
