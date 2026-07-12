"""Signals — the cached read path (function b).

GET /v1/signals/{exchange}/{symbol}
    → returns TerseResponsePayload by default
    → ?verbose=true returns VerboseResponsePayload (single latest bar)

GET /v1/signals/{exchange}/{symbol}/history
    → returns the full bar-by-bar indicator state over a date range,
      with overlapping signal_events for the chart's marker layer.

Reads from SignalCache only. Does NOT trigger a compute on miss — that
would create a second writer to the cache. If the ticker isn't primed,
returns 404.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, UTC

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Engine, text

# PHASE-3E: SignalCache + get_cache imports removed (both were dead —
# no route handler in this module actually consumed them).
from ....api import TickerNotPrimedError
from ....domain import TickerRef, Timeframe
from ..dependencies import get_db_engine
from .signal_history import build_signal_history_payload
from ..schemas import (
    HistoricalBarPayload,
    SignalHistoryPayload,
    TerseResponsePayload,
    VerboseResponsePayload,
)
from ....persistence import TickersRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/signals", tags=["signals"])

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
    `industry_indicator_state` rows for this industry. That table is DAILY
    grain, so each week is collapsed to its LAST daily row — the converged
    end-of-week state — before being joined onto the weekly bar. Signal
    events list is empty: signals are ticker-only by design.

    Returns 404 if the industry is unknown. Returns an empty bars
    list (with notes) if the industry has no values in the requested
    window.
    """
    with db_engine.connect() as conn:
        # No hard-gate on industry_index existence — the table may
        # not have a row for this industry if build-industry-indices
        # hasn't been run yet, but industry_index_value may still
        # have data (or we may want to return an empty-bars payload
        # so the GUI degrades gracefully instead of showing "fetch
        # failed").
        since = datetime.now(UTC) - timedelta(days=365 * years)
        # DROPPED-TABLE-FIX: `indicator_state` was dropped in migration 0032
        # (and its `trade_date` column with it), so this raised
        # "relation indicator_state does not exist" every time the Industry
        # panel opened. Replacement is industry_indicator_state, which is DAILY
        # grain (migration 0035): ~5 rows per week, all sharing the week's
        # IST-Monday bar_timestamp.
        #
        # This is a WEEKLY chart, so it needs exactly ONE indicator row per
        # week — the week's LAST daily row (the converged end-of-week state).
        # Joining the daily-grain table directly would fan every daily index
        # value out across all ~5 state rows of its week.
        #
        # bar_timestamp is `timestamp` (no tz) in this table, so ::date is a
        # plain cast with no session-timezone trap.
        rows = conn.execute(text("""
            WITH weekly_state AS (
                SELECT DISTINCT ON (industry_name, bar_timestamp)
                       industry_name, bar_timestamp,
                       med_ema, long_ema, longest_ema, atr,
                       upper_inner_band, lower_inner_band,
                       upper_mid_band,   lower_mid_band,
                       upper_outer_band, lower_outer_band,
                       display_regime
                  FROM industry_indicator_state
                 WHERE timeframe = '1W'
                 ORDER BY industry_name, bar_timestamp, date DESC
            )
            SELECT
                v.trade_date,
                v.value,
                s.med_ema, s.long_ema, s.longest_ema,
                s.atr,
                s.upper_inner_band, s.lower_inner_band,
                s.upper_mid_band,   s.lower_mid_band,
                s.upper_outer_band, s.lower_outer_band,
                s.display_regime
            FROM industry_index_value v
            LEFT JOIN weekly_state s
                   ON s.industry_name = v.industry_name
                  AND s.bar_timestamp::date
                      = DATE_TRUNC('week', v.trade_date)::date
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
    weeks: OrderedDict[date, list] = OrderedDict()
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
        # Unified regime: core_bg == display_bg at the wire boundary.
        # Field kept for backward compat with consumers that read it.
        core_bg = display_bg

        # bar_timestamp: encode the Monday as a UTC datetime at 00:00.
        # Same shape as the ticker endpoint's bar_timestamps.
        bar_ts = datetime.combine(
            week_start, datetime.min.time(), tzinfo=UTC,
        )
        bars.append(HistoricalBarPayload(
            bar_timestamp=bar_ts,
            open=open_, high=high, low=low, close=close,
            volume=1.0,
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

@router.get(
    "/{exchange}/{symbol}/history",
    response_model=SignalHistoryPayload,
)
def get_signal_history(
    exchange: str, symbol: str,
    instrument: str = "EQ",
    years: int = 3,
    db_engine: Engine = Depends(get_db_engine),
) -> SignalHistoryPayload:
    ref = TickerRef(exchange=exchange, symbol=symbol, instrument=instrument)
    with db_engine.connect() as conn:
        ticker = TickersRepository(conn).get_by_ref(ref)
        if ticker is None:
            raise HTTPException(404, f"ticker not found: {exchange}/{symbol}")
        return build_signal_history_payload(
            conn,
            ticker_id=ticker.ticker_id,
            ticker_ref=ref,
            timeframe=Timeframe.WEEKLY,
            years_back=years,
        )

# === STEP 15-B8-endpoint: get_signal endpoint removed ===
# The cached read path depended on the legacy SignalCache.latest_signal_event
# field which has been demolished. Consumers needing the latest signal can
# either query /v1/signals/{ex}/{sym}/history or use V1 endpoints.


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