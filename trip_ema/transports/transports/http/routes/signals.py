"""Signals — the cached read path (function b).

GET /v1/signals/{exchange}/{symbol}
    → returns TerseResponsePayload by default
    → ?verbose=true returns VerboseResponsePayload with last_n_bars

Reads from SignalCache only. Does NOT trigger a compute on miss — that
would create a second writer to the cache. If the ticker isn't primed,
returns 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ....api import (
    TickerNotPrimedError,
    query_cached_terse,
    query_cached_verbose,
)
from ....cache import SignalCache
from ....domain import TickerRef, Timeframe
from ..dependencies import get_cache
from ..schemas import TerseResponsePayload, VerboseResponsePayload

router = APIRouter(prefix="/v1/signals", tags=["signals"])


@router.get(
    "/{exchange}/{symbol}",
    response_model=TerseResponsePayload | VerboseResponsePayload,
)
def get_signal(
    exchange: str,
    symbol: str,
    instrument: str = Query(default="EQ", max_length=16),
    timeframe: Timeframe = Query(default=Timeframe.WEEKLY),
    verbose: bool = Query(default=False),
    last_n_bars: int = Query(default=200, ge=1, le=1000),
    cache: SignalCache = Depends(get_cache),
):
    """Return the latest cached signal for this ticker × timeframe."""
    ref = TickerRef(exchange=exchange, symbol=symbol, instrument=instrument)
    try:
        if verbose:
            internal = query_cached_verbose(
                ticker=ref, timeframe=timeframe,
                cache=cache, last_n_bars=last_n_bars,
            )
            return VerboseResponsePayload.from_domain(internal)
        internal_terse = query_cached_terse(
            ticker=ref, timeframe=timeframe, cache=cache,
        )
        return TerseResponsePayload.from_domain(internal_terse)
    except TickerNotPrimedError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e),
        )
