"""Daily-grain weekly-indicator computation (multi-market).

Weekly formulas sampled once per trading day. For a given day D the
currently-forming week is a synthetic weekly bar (week-to-date OHLC;
volume projected to a full-week estimate by the exchange's trading-
hours elapsed). Appending it to the completed weekly bars and reading
the last row gives day D's snapshot (trade_date=D, bar_timestamp=that
week's Monday).

The compute is done by the O(weeks+days) state-carry engine
(`state_carry_engine.compute_daily_grain_via_engine`), which is
bit-faithful to the archived pandas path (parity-gated). State and
enrichment come from the SAME walk.
"""
from __future__ import annotations

from datetime import date, datetime

from ..domain import OHLCVBar
from .indicator_primitives import INDICATOR_DEFAULTS
from ..domain.market_session import MarketSession
from .state_carry_engine import compute_daily_grain_via_engine


def synthesize_forming_bar(
    week_daily_bars: list[OHLCVBar],
    w_monday_utc: datetime,
    monday_local: date,
    as_of: date,
    now_local: datetime,
    session: MarketSession,
) -> OHLCVBar:
    """Synthetic forming-week bar for `as_of`: week-to-date OHLC, volume
    prorated by the session's trading-hours elapsed. Anchored at the
    week's Monday (UTC). Retained as a pure helper (used by tests and
    callers that want the forming bar directly)."""
    open_ = week_daily_bars[0].open
    high = max(b.high for b in week_daily_bars)
    low = min(b.low for b in week_daily_bars)
    close = week_daily_bars[-1].close
    wtd_volume = sum(b.volume for b in week_daily_bars)
    volume = session.imputed_weekly_volume(
        wtd_volume, monday_local, as_of, now_local,
    )
    return OHLCVBar(
        timestamp=w_monday_utc,
        open=open_, high=high, low=low, close=close, volume=volume,
    )


def compute_daily_grain_states(
    daily_bars: list[OHLCVBar],
    *,
    session: MarketSession,
    now: datetime,
    target_days: list[date] | None = None,
) -> list[dict]:
    """One state primitive dict per target trading day (trade_date,
    bar_timestamp + 12 core fields), via the state-carry engine."""
    state_rows, _ = compute_daily_grain_via_engine(
        daily_bars, session=session, now=now,
        params=INDICATOR_DEFAULTS, target_days=target_days,
    )
    return state_rows


def compute_daily_grain_enrichment(
    daily_bars: list[OHLCVBar],
    *,
    session: MarketSession,
    now: datetime,
    target_days: list[date] | None = None,
) -> list[dict]:
    """One enrichment primitive dict per target trading day, aligned 1:1
    with `compute_daily_grain_states` (same walk, same days survive
    warmup), via the state-carry engine."""
    _, enrich_rows = compute_daily_grain_via_engine(
        daily_bars, session=session, now=now,
        params=INDICATOR_DEFAULTS, target_days=target_days,
    )
    return enrich_rows
