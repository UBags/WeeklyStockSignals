"""Commit 1, patch 04.

Edit `trip_ema/transports/http/routes/fundamentals.py`:
  1. Delete the `GET /signal-events/industries/{industry_name:path}`
     endpoint and its `get_industry_signal_events` handler.
  2. Strip industry params/branches from `_run_signals_safe`.
  3. Strip industry query params from the `POST /compute-signals`
     endpoint (`compute_signals_endpoint`).

The cross-industry scanner `GET /signal-events/recent` stays — it just
reads what the repo returns; with no new industry rows it returns
ticker events only. Harmless.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _patch_lib import edit, run as _run


TARGET = "trip_ema/transports/http/routes/fundamentals.py"


# ---- Edit 1: delete GET /signal-events/industries/{industry_name:path} ----

OLD_GET_INDUSTRY_ENDPOINT = '''@router.get("/signal-events/industries/{industry_name:path}",
            response_model=SignalEventListPayload)
def get_industry_signal_events(
    industry_name: str,
    kind: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=5000),
    db_engine: Engine = Depends(get_db_engine),
) -> SignalEventListPayload:
    """Get signal events for one industry, newest-first.

    Pass kind=BUY|SELL|EXIT_BUY|EXIT_SELL to filter by event kind.
    Pass from_date/to_date to restrict the date range, and limit to
    cap the row count.
    """
    from ....fundamentals import SIGNAL_EVENT_KINDS
    if kind is not None and kind not in SIGNAL_EVENT_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"kind must be one of {SIGNAL_EVENT_KINDS}, "
                   f"got {kind!r}",
        )
    with db_engine.connect() as conn:
        rows = SignalEventRepository(conn).get_for_industry(
            industry_name,
            kinds=[kind] if kind else None,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
    return SignalEventListPayload(
        industry_name=industry_name,
        from_date=from_date,
        to_date=to_date,
        kind=kind,
        rows=[_signal_event_payload(e) for e in rows],
    )


'''  # Trailing blank line so we don't leave a triple-blank


# ---- Edit 2: rewrite _run_signals_safe to drop industry branches ----

OLD_RUN_SIGNALS_SAFE = '''def _run_signals_safe(
    db_engine: Engine, industry_name: Optional[str],
    force: bool, all_industries: bool,
    ticker: Optional[str] = None,
    all_tickers: bool = False,
    exchange: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> None:
    from ....fundamentals.runners import (
        run_signal_events_for_all_industries,
        run_signal_events_for_all_tickers,
        run_signal_events_for_industry,
        run_signal_events_for_ticker,
    )
    from ....persistence.tickers_repo import TickersRepository
    from ....domain import TickerRef
    try:
        if all_industries:
            run_signal_events_for_all_industries(db_engine, force=force)
        elif all_tickers:
            run_signal_events_for_all_tickers(
                db_engine, timeframe, exchange=exchange, force=force,
            )
        elif ticker:
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
            run_signal_events_for_industry(
                db_engine, industry_name, force=force,
            )
    except Exception:
        logger.exception(
            "Signal events compute failed for industry=%s "
            "all_industries=%s ticker=%s all_tickers=%s",
            industry_name, all_industries, ticker, all_tickers,
        )'''

NEW_RUN_SIGNALS_SAFE = '''def _run_signals_safe(
    db_engine: Engine,
    force: bool,
    ticker: Optional[str] = None,
    all_tickers: bool = False,
    exchange: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> None:
    from ....fundamentals.runners import (
        run_signal_events_for_all_tickers,
        run_signal_events_for_ticker,
    )
    from ....persistence.tickers_repo import TickersRepository
    from ....domain import TickerRef
    try:
        if all_tickers:
            run_signal_events_for_all_tickers(
                db_engine, timeframe, exchange=exchange, force=force,
            )
        elif ticker:
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
        )'''


# ---- Edit 3: rewrite POST /compute-signals to drop industry params ----

OLD_COMPUTE_SIGNALS_ENDPOINT = '''@router.post("/compute-signals",
             response_model=RefreshAcceptedPayload, status_code=202)
def compute_signals_endpoint(
    industry: Optional[str] = Query(None),
    all_industries: bool = Query(False),
    ticker: Optional[str] = Query(None),
    all_tickers: bool = Query(False),
    exchange: Optional[str] = Query(None),
    timeframe: Optional[str] = Query(None),
    force: bool = Query(False),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
    executor = Depends(get_etl_executor),
) -> RefreshAcceptedPayload:
    """Fire-and-forget signal events compute. Modes (mutually
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
        "Admin: compute-signals requested",
        extra={"industry": industry, "all_industries": all_industries,
               "ticker": ticker, "all_tickers": all_tickers,
               "exchange": exchange, "timeframe": timeframe,
               "force": force},
    )
    executor.submit(
        _run_signals_safe, db_engine,
        industry, force, all_industries,
        ticker, all_tickers, exchange, timeframe,
    )
    return RefreshAcceptedPayload(
        action="compute-signals",
        run_id=None,
        accepted_at=datetime.now(timezone.utc),
        notes=(
            f"Signal events compute (industry={industry!r}, "
            f"all_industries={all_industries}, ticker={ticker!r}, "
            f"all_tickers={all_tickers}, timeframe={timeframe!r}) "
            f"submitted."
        ),
    )'''

NEW_COMPUTE_SIGNALS_ENDPOINT = '''@router.post("/compute-signals",
             response_model=RefreshAcceptedPayload, status_code=202)
def compute_signals_endpoint(
    ticker: Optional[str] = Query(None),
    all_tickers: bool = Query(False),
    exchange: Optional[str] = Query(None),
    timeframe: Optional[str] = Query(None),
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
        accepted_at=datetime.now(timezone.utc),
        notes=(
            f"Signal events compute (ticker={ticker!r}, "
            f"all_tickers={all_tickers}, timeframe={timeframe!r}) "
            f"submitted."
        ),
    )'''


def patch(text: str) -> str:
    text = edit(text, OLD_GET_INDUSTRY_ENDPOINT, "")
    text = edit(text, OLD_RUN_SIGNALS_SAFE, NEW_RUN_SIGNALS_SAFE)
    text = edit(text, OLD_COMPUTE_SIGNALS_ENDPOINT, NEW_COMPUTE_SIGNALS_ENDPOINT)
    return text


if __name__ == "__main__":
    _run(TARGET, patch)
