"""Single source of truth for forming-weekly-bar construction (Option A).

The daily-grain engine walk and the signal-enrichment walk MUST see the
identical sequence of weekly bars, or the stored EMAs and the freshly
computed §4 series would silently sit on different weekly closes. This
generator owns that construction once; both drivers consume it.

For each week (in order) it yields the per-day *forming* bars — the
week-to-date OHLC as of each day plus the imputed weekly volume — and the
real full-volume weekly bar used to advance past the week. Extracted
verbatim from compute_daily_grain_via_engine; behaviour is unchanged.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from collections.abc import Iterator

from ..domain import OHLCVBar
from ..indicators.aggregation import _monday_of, daily_to_weekly
from ..domain.market_session import MarketSession


@dataclass(frozen=True, slots=True)
class FormingDayBar:
    """Week-to-date bar as of `date` (open=week first, high/low=running
    extrema, close=this day's close, volume=imputed weekly volume)."""
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class FormingWeek:
    """One week of the walk. `days` are the (target-filtered) forming
    bars to branch off the week-start snapshot; `real`/`real_vol` is the
    real full-volume weekly bar to advance past the week (None if the
    week is not yet complete)."""
    monday: datetime            # UTC Monday 00:00 — the bar_timestamp
    monday_local: date          # Monday in the exchange-local zone
    days: list                  # list[FormingDayBar], in order
    real: OHLCVBar | None
    real_vol: float | None


def weekly_forming_walk(
    daily_bars: list[OHLCVBar],
    *,
    session: MarketSession,
    now: datetime,
    target_days: list[date] | None = None,
    constant_volume: float | None = None,
) -> Iterator[FormingWeek]:
    """Yield the forming-weekly-bar walk, week by week, in order."""
    if not daily_bars:
        return

    zone = session.zone
    now_local = now.astimezone(zone)

    completed = daily_to_weekly(
        daily_bars, include_partial_last_week=False, tz=session.tz,
    )
    real_by_monday = {wb.timestamp: wb for wb in completed}

    by_week: dict = defaultdict(list)
    for b in daily_bars:
        w = _monday_of(b.timestamp, tz=session.tz)
        by_week[w].append((b.timestamp.astimezone(zone).date(), b))
    for w in by_week:
        by_week[w].sort(key=lambda p: p[0])
    weeks_in_order = sorted(by_week.keys())

    tset = set(target_days) if target_days is not None else None

    for w_monday in weeks_in_order:
        monday_local = w_monday.astimezone(zone).date()
        week_pairs = by_week[w_monday]

        days: list = []
        for (ld, _b) in week_pairs:
            if tset is not None and ld not in tset:
                continue
            week_daily = [b for (dloc, b) in week_pairs if dloc <= ld]
            open_ = week_daily[0].open
            hi = max(b.high for b in week_daily)
            lo = min(b.low for b in week_daily)
            cl = week_daily[-1].close
            wtd_vol = sum(b.volume for b in week_daily)
            if constant_volume is not None:
                vol = constant_volume
            else:
                vol = session.imputed_weekly_volume(
                    wtd_vol, monday_local, ld, now_local,
                )
            days.append(FormingDayBar(
                date=ld, open=open_, high=hi, low=lo, close=cl, volume=vol,
            ))

        real = real_by_monday.get(w_monday)
        if real is not None:
            real_vol = (constant_volume if constant_volume is not None
                        else real.volume)
        else:
            real_vol = None

        yield FormingWeek(
            monday=w_monday, monday_local=monday_local,
            days=days, real=real, real_vol=real_vol,
        )
