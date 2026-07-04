"""Daily-grain weekly-indicator computation (multi-market).

Weekly formulas sampled once per trading day. For a given day D the
currently-forming week is a *synthetic weekly bar*:

  * OHLC   = week-to-date (open = week's first open, high/low = running
             extremes, close = D's close),
  * volume = week-to-date volume projected to a full-week estimate by
             the exchange's trading-hours elapsed (see
             MarketSession.imputed_weekly_volume).

That synthetic bar is appended to the *completed* weekly bars (real
full-volume aggregates, unchanged) and the whole array is run through
the existing weekly compute. The synthetic bar's row is day D's
snapshot, stored with trade_date=D and bar_timestamp=that week's Monday.

The single hours-elapsed formula subsumes every case and every market
because the session length and trading calendar come from the
`MarketSession`, not hardcoded constants:

    NSE  (6.25h session): end of session k -> x (k_sessions_total / k)
    NYSE (6.50h session): identical shape, different denominator hours
    holiday-shortened week -> fewer sessions -> denominator shrinks
    live intraday mid-session -> partial hours via wall-clock `now`
    backfill/EoD -> `as_of` is a closed day -> lands on session boundary

State and enrichment share the same per-day array iterator; only the
primitive function differs (`compute_state_primitives` vs
`compute_enrichment_primitives`).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta

import pandas as pd

from ..domain import OHLCVBar
from ..indicators.aggregation import _monday_of, daily_to_weekly
from .market_session import MarketSession
from .v1_compute_primitives import (
    compute_enrichment_primitives,
    compute_state_primitives,
)


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
    week's Monday (UTC) to match the completed weekly bars."""
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


def _iter_forming_arrays(
    daily_bars: list[OHLCVBar],
    session: MarketSession,
    now: datetime,
    target_days: list[date] | None,
) -> Iterator[tuple[date, datetime, list[OHLCVBar]]]:
    """Yield (trade_date, w_monday_utc, weekly_array) per target day,
    where weekly_array = completed weeks before this week + the
    prorated synthetic forming bar."""
    zone = session.zone
    now_local = now.astimezone(zone)

    completed = sorted(
        daily_to_weekly(
            daily_bars, include_partial_last_week=False, tz=session.tz,
        ),
        key=lambda wb: wb.timestamp,
    )

    by_week: dict[datetime, list[tuple[date, OHLCVBar]]] = defaultdict(list)
    for b in daily_bars:
        w = _monday_of(b.timestamp, tz=session.tz)
        local_d = b.timestamp.astimezone(zone).date()
        by_week[w].append((local_d, b))
    for w in by_week:
        by_week[w].sort(key=lambda pair: pair[0])

    all_days = sorted(
        b.timestamp.astimezone(zone).date() for b in daily_bars
    )
    days = sorted(set(target_days)) if target_days is not None else all_days

    for d in days:
        monday_local = d - timedelta(days=d.weekday())
        w_monday_utc = _monday_of(
            datetime.combine(d, time.min, tzinfo=zone), tz=session.tz,
        )
        week_pairs = by_week.get(w_monday_utc, [])
        week_daily = [b for (ld, b) in week_pairs if ld <= d]
        if not week_daily:
            continue
        forming = synthesize_forming_bar(
            week_daily, w_monday_utc, monday_local, d, now_local, session,
        )
        prefix = [wb for wb in completed if wb.timestamp < w_monday_utc]
        yield d, w_monday_utc, prefix + [forming]


def _compute_daily_grain(
    daily_bars: list[OHLCVBar],
    session: MarketSession,
    now: datetime,
    target_days: list[date] | None,
    primitive_fn: Callable[..., list[dict]],
) -> list[dict]:
    """Run `primitive_fn` per target day over that day's forming array;
    keep only the forming bar's row, tagged with trade_date. Days still
    inside EMA warmup (forming row is NaN and thus dropped by the
    primitive function) produce no row."""
    if not daily_bars:
        return []

    out: list[dict] = []
    for d, w_monday_utc, array in _iter_forming_arrays(
        daily_bars, session, now, target_days,
    ):
        close_s = pd.Series([b.close for b in array], dtype=float)
        volume_s = pd.Series([b.volume for b in array], dtype=float)
        high_s = pd.Series([b.high for b in array], dtype=float)
        low_s = pd.Series([b.low for b in array], dtype=float)
        ts_s = pd.Series([b.timestamp for b in array])

        prims = primitive_fn(
            close_s, volume_s, high_s, low_s,
            trade_dates=ts_s,
            synthetic_atr_for_index=False,
        )
        forming_prim = next(
            (p for p in prims if p["bar_timestamp"] == w_monday_utc),
            None,
        )
        if forming_prim is None:
            continue
        row = dict(forming_prim)
        row["trade_date"] = d
        out.append(row)

    return out


def compute_daily_grain_states(
    daily_bars: list[OHLCVBar],
    *,
    session: MarketSession,
    now: datetime,
    target_days: list[date] | None = None,
) -> list[dict]:
    """One state primitive dict per target trading day. Each carries
    trade_date (the day) and bar_timestamp (the week Monday) plus the 12
    core indicator fields."""
    return _compute_daily_grain(
        daily_bars, session, now, target_days, compute_state_primitives,
    )


def compute_daily_grain_enrichment(
    daily_bars: list[OHLCVBar],
    *,
    session: MarketSession,
    now: datetime,
    target_days: list[date] | None = None,
) -> list[dict]:
    """One enrichment primitive dict per target trading day, aligned 1:1
    with `compute_daily_grain_states` (same days survive warmup), so the
    enrichment FK to stock_indicator_state is satisfied per row."""
    return _compute_daily_grain(
        daily_bars, session, now, target_days,
        compute_enrichment_primitives,
    )
