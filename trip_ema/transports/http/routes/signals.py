"""Signals — the cached read path (function b).

GET /v1/signals/{exchange}/{symbol}
    → returns TerseResponsePayload by default
    → ?verbose=true returns VerboseResponsePayload with last_n_bars

GET /v1/signals/{exchange}/{symbol}/history
    → returns the full bar-by-bar indicator state over a date range,
      with overlapping signal_events for the chart's marker layer.

Reads from SignalCache only. Does NOT trigger a compute on miss — that
would create a second writer to the cache. If the ticker isn't primed,
returns 404.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Engine, text

from ....api import (
    TickerNotPrimedError,
    query_cached_terse,
    query_cached_verbose,
)
from ....cache import SignalCache
from ....config import Config
from ....domain import OHLCVBar, TickerRef, Timeframe
from ....engine import TickerEngine
from ....indicators.aggregation import daily_to_weekly
from ..dependencies import get_cache, get_config, get_db_engine
from ..schemas import (
    HistoricalBarPayload,
    HistoricalSignalEventPayload,
    SignalHistoryPayload,
    TerseResponsePayload,
    VerboseResponsePayload,
)

logger = logging.getLogger(__name__)

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


@router.get(
    "/{exchange}/{symbol}/history",
    response_model=SignalHistoryPayload,
)
def get_signal_history(
    exchange: str,
    symbol: str,
    instrument: str = Query(default="EQ", max_length=16),
    years: int = Query(
        default=3, ge=1, le=20,
        description="How many years of history to return. 1/3/5/10/20 are typical."
    ),
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> SignalHistoryPayload:
    """Return the full bar-by-bar indicator state for the chart.

    Pulls raw daily bars from `ohlcv_bars`, aggregates to weekly using
    the same `daily_to_weekly` helper the scheduler uses, runs a
    fresh TickerEngine over them to materialize each bar's state,
    and joins with the matching `signal_events` rows.

    This endpoint is the canonical 'GUI replays the engine'
    implementation (per Slice 3 option C). The GUI process itself
    never imports `trip_ema.engine`.

    Compute cost: ~50-100ms for a 10-year (520-bar) request including
    DB IO. Cached at zero levels — re-runs on each call. If you find
    yourself hitting this endpoint at high rate, the next step is to
    materialize indicator values into a new DB table; for now,
    on-demand recompute is fine.
    """
    # Step 1: resolve (exchange, symbol, instrument) → ticker_id + tz.
    # We deliberately ignore tickers.source_timeframe and instead look
    # at what bars actually exist for this ticker — the source_timeframe
    # column isn't kept in sync by the ETL and lies about the data.
    # The bars table is the only ground truth.
    with db_engine.connect() as conn:
        row = conn.execute(text("""
                SELECT ticker_id, COALESCE(tz, 'UTC') AS tz
                FROM tickers
                WHERE exchange = :ex AND symbol = :sym AND instrument = :inst
            """), {"ex": exchange, "sym": symbol, "inst": instrument}).first()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ticker {exchange}/{symbol}/{instrument} not found",
            )
        ticker_id = row.ticker_id
        tz = row.tz

        # Discover what timeframes actually have bars for this ticker.
        # Pick the finest available source we know how to aggregate from.
        # Order matters: 1W → use directly; finer → aggregate via
        # daily_to_weekly() which handles any sub-weekly source.
        available_rows = conn.execute(text("""
                SELECT DISTINCT timeframe
                FROM ohlcv_bars
                WHERE ticker_id = :tid
            """), {"tid": ticker_id}).all()
        available_tfs = {r.timeframe for r in available_rows}

        if "1W" in available_tfs:
            source_tf = "1W"
        elif "1D" in available_tfs:
            source_tf = "1D"
        elif "2h" in available_tfs:
            source_tf = "2h"
        elif "1h" in available_tfs:
            source_tf = "1h"
        elif "15m" in available_tfs:
            source_tf = "15m"
        else:
            # No bars at all in any usable timeframe
            return SignalHistoryPayload(
                exchange=exchange, symbol=symbol, instrument=instrument,
                timeframe="1W",
                bars=[], signal_events=[],
                warmup_bars_count=0,
                notes="No bars in any usable timeframe for this ticker.",
            )

        # Step 2: fetch raw bars at the discovered source timeframe.
        warmup_pad_days = 400
        window_start = datetime.now(timezone.utc) - timedelta(
            days=365 * years + warmup_pad_days,
        )
        rows = conn.execute(text("""
                SELECT bar_timestamp, open, high, low, close, volume
                FROM ohlcv_bars
                WHERE ticker_id = :tid AND timeframe = :tf
                  AND bar_timestamp >= :since
                ORDER BY bar_timestamp ASC
            """), {"tid": ticker_id, "tf": source_tf, "since": window_start})
        raw_bars = [
            OHLCVBar(
                timestamp=r.bar_timestamp,
                open=r.open, high=r.high, low=r.low,
                close=r.close, volume=r.volume,
            )
            for r in rows
        ]

        # Step 3: matching signal_events. We pull all events whose
        # bar_timestamp lies within the unpadded user window — the
        # padded warmup region is internal and shouldn't surface
        # markers from before the user-requested view.
        user_window_start = datetime.now(timezone.utc) - timedelta(days=365 * years)
        event_rows = conn.execute(text("""
            SELECT event_id, bar_timestamp, signal_type, price_at_signal
            FROM signal_events
            WHERE ticker_id = :tid
              AND bar_timestamp >= :since
            ORDER BY bar_timestamp ASC
        """), {"tid": ticker_id, "since": user_window_start})
        signal_events = [
            HistoricalSignalEventPayload(
                event_id=e.event_id,
                bar_timestamp=e.bar_timestamp,
                signal=e.signal_type,
                price_at_signal=float(e.price_at_signal),
            )
            for e in event_rows
        ]

    if not raw_bars:
        return SignalHistoryPayload(
            exchange=exchange, symbol=symbol, instrument=instrument,
            timeframe="1W",
            bars=[], signal_events=[],
            warmup_bars_count=0,
            notes="No bars in the requested window.",
        )

    # Step 4: aggregate to weekly if needed. If the source was already
    # weekly, use the raw bars directly.
    if source_tf == "1W":
        weekly_bars = raw_bars
    else:
        weekly_bars = daily_to_weekly(
            raw_bars, include_partial_last_week=True, tz=tz,
        )

    # Step 5: build a fresh engine and replay. history_retain = len
    # + small margin so we keep every state for response building.
    ref = TickerRef(exchange=exchange, symbol=symbol, instrument=instrument)
    params = config.to_indicator_params()
    engine = TickerEngine(
        ref, Timeframe.WEEKLY, params,
        history_retain=len(weekly_bars) + 10,
    )
    engine.prime(weekly_bars)
    all_states = engine.state_history(n=len(weekly_bars) + 10)

    # Step 6: zip states with their original bars (one-to-one) and
    # filter to the user window.
    bars_by_ts = {b.timestamp: b for b in weekly_bars}
    payload_bars: list[HistoricalBarPayload] = []
    warmup_count = 0
    for st in all_states:
        if st.timestamp < user_window_start:
            continue  # warmup region; not returned
        original = bars_by_ts.get(st.timestamp)
        if original is None:
            # Shouldn't happen — state timestamps always come from bars
            continue
        is_warm = st.is_warmed_up
        if not is_warm:
            warmup_count += 1
        payload_bars.append(HistoricalBarPayload(
            bar_timestamp=st.timestamp,
            open=original.open, high=original.high,
            low=original.low, close=original.close,
            volume=original.volume,
            med_ema=_finite_or_none(st.med_ema),
            long_ema=_finite_or_none(st.long_ema),
            longest_ema=_finite_or_none(st.longest_ema),
            atr=_finite_or_none(st.atr),
            upper_inner=_finite_or_none(st.upper_inner),
            lower_inner=_finite_or_none(st.lower_inner),
            upper_mid=_finite_or_none(st.upper_mid),
            lower_mid=_finite_or_none(st.lower_mid),
            upper_outer=_finite_or_none(st.upper_outer),
            lower_outer=_finite_or_none(st.lower_outer),
            core_bg=st.core_bg.name,
            display_bg=st.display_bg.name,
            is_warmed_up=is_warm,
        ))

    return SignalHistoryPayload(
        exchange=exchange, symbol=symbol, instrument=instrument,
        timeframe="1W",
        bars=payload_bars,
        signal_events=signal_events,
        warmup_bars_count=warmup_count,
        notes=(
            f"Replayed engine over {len(weekly_bars)} weekly bars; "
            f"returning {len(payload_bars)} bars in the user window."
        ),
    )


def _finite_or_none(v: float) -> float | None:
    """Pine warmup produces NaN; JSON can't carry NaN, so we map to
    None. The chart layer treats None as 'don't plot this point on
    that line', which is what we want — the indicator lines start
    drawing only after the warmup period."""
    import math
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return float(v)
