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
    # Discover from ohlcv_bars what timeframes actually exist for
    # this ticker. A `tickers.source_timeframe` column existed
    # pre-Phase-13 but was removed in migration 0003 — it was never
    # kept in sync by the ETL.
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


@router.get(
    "/industry/{industry_name}/history",
    response_model=SignalHistoryPayload,
)
def get_industry_history(
    industry_name: str,
    years: int = Query(
        default=3, ge=1, le=20,
        description="How many years of history to return.",
    ),
    db_engine: Engine = Depends(get_db_engine),
) -> SignalHistoryPayload:
    """Industry-index history with synthetic ±0.25% OHLC candles.

    The industry index is a daily-value series (`industry_index_value`),
    not OHLC. For chart consistency we synthesize a weekly bar per
    Pine-style Monday-IST-anchored week:
      - close   = last day's index value in the week
      - delta   = 0.0025 * close                  (the ±0.25% body)
      - direction = sign(close - prev_week_close)
      - up week  : open = close - delta,  high = close + delta, low = open
      - down week: open = close + delta,  low  = close - delta, high = open
    Volume is reported as 0 (industries don't have volume).

    Indicator overlays (EMAs / bands / regime) come from
    `indicator_state` rows for this industry, sampled at the last
    trading day of each week. Signal events list is empty —
    `signal_events` is ticker-only by design.

    Returns 404 if the industry is unknown. Returns an empty bars
    list (with notes) if the industry has no values in the requested
    window.
    """
    with db_engine.connect() as conn:
        exists = conn.execute(text("""
            SELECT 1 FROM industry_index WHERE industry_name = :n
        """), {"n": industry_name}).first()
        if exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Industry index '{industry_name}' not found",
            )

        # Pull daily values + the SAME-day indicator_state for the
        # window. Joining left-side so weeks without indicator_state
        # (warmup) still show candles.
        since = datetime.now(timezone.utc) - timedelta(days=365 * years)
        rows = conn.execute(text("""
            SELECT
                v.trade_date,
                v.value,
                s.med_ema, s.long_ema, s.longest_ema,
                s.atr,
                s.upper_inner_band, s.lower_inner_band,
                s.upper_mid_band,   s.lower_mid_band,
                s.upper_outer_band, s.lower_outer_band,
                s.display_regime, s.core_regime
            FROM industry_index_value v
            LEFT JOIN indicator_state s
                   ON s.industry_name = v.industry_name
                  AND s.trade_date = v.trade_date
            WHERE v.industry_name = :n
              AND v.trade_date >= :since
            ORDER BY v.trade_date ASC
        """), {"n": industry_name, "since": since.date()}).all()

    if not rows:
        return SignalHistoryPayload(
            exchange="INDEX", symbol=industry_name, instrument="INDUSTRY",
            timeframe="1W",
            bars=[], signal_events=[],
            warmup_bars_count=0,
            notes="No values for this industry in the requested window.",
        )

    # Group rows by IST-Monday-anchored week. Use the trade_date
    # directly (no tz on DATE columns); a date d sits in the week
    # starting at the Monday on-or-before d.
    from collections import OrderedDict
    weeks: "OrderedDict[date, list]" = OrderedDict()
    for r in rows:
        d = r.trade_date
        # Monday-of-week. weekday(): Mon=0..Sun=6.
        week_start = d - timedelta(days=d.weekday())
        weeks.setdefault(week_start, []).append(r)

    # Synthesize per-week bars. Iterate so we can carry prev_close.
    bars: list[HistoricalBarPayload] = []
    warmup_count = 0
    prev_close: float | None = None
    for week_start, week_rows in weeks.items():
        # Last day's row carries this week's close AND the indicator
        # values we sample for display.
        last = week_rows[-1]
        close = float(last.value)
        delta = 0.0025 * close
        if prev_close is None or close == prev_close:
            # First week or flat — render as a doji-ish up bar so
            # the body still has a body. Open=close-delta, low=open,
            # high=close+delta.
            open_ = close - delta
            high = close + delta
            low = open_
        elif close > prev_close:
            open_ = close - delta
            high = close + delta
            low = open_
        else:
            open_ = close + delta
            high = open_
            low = close - delta

        is_warmed_up = last.med_ema is not None
        if not is_warmed_up:
            warmup_count += 1

        # Map display_regime SMALLINT (-1/0/+1) to the same enum-name
        # strings the ticker endpoint uses.
        display_bg = (
            "BULLISH" if last.display_regime == 1
            else "BEARISH" if last.display_regime == -1
            else "NEUTRAL"
        )
        core_bg = (
            "BULLISH" if last.core_regime == 1
            else "BEARISH" if last.core_regime == -1
            else "NEUTRAL"
        )

        # bar_timestamp: encode the Monday as a UTC datetime at 00:00.
        # Same shape as the ticker endpoint's bar_timestamps.
        bar_ts = datetime.combine(
            week_start, datetime.min.time(), tzinfo=timezone.utc,
        )
        bars.append(HistoricalBarPayload(
            bar_timestamp=bar_ts,
            open=open_, high=high, low=low, close=close,
            volume=0.0,
            med_ema=_finite_or_none(last.med_ema),
            long_ema=_finite_or_none(last.long_ema),
            longest_ema=_finite_or_none(last.longest_ema),
            atr=_finite_or_none(last.atr),
            upper_inner=_finite_or_none(last.upper_inner_band),
            lower_inner=_finite_or_none(last.lower_inner_band),
            upper_mid=_finite_or_none(last.upper_mid_band),
            lower_mid=_finite_or_none(last.lower_mid_band),
            upper_outer=_finite_or_none(last.upper_outer_band),
            lower_outer=_finite_or_none(last.lower_outer_band),
            core_bg=core_bg,
            display_bg=display_bg,
            is_warmed_up=is_warmed_up,
        ))
        prev_close = close

    return SignalHistoryPayload(
        exchange="INDEX", symbol=industry_name, instrument="INDUSTRY",
        timeframe="1W",
        bars=bars,
        signal_events=[],
        warmup_bars_count=warmup_count,
        notes=(
            f"Synthesized {len(bars)} weekly bars from "
            f"{len(rows)} daily values; +/-0.25% body width."
        ),
    )
