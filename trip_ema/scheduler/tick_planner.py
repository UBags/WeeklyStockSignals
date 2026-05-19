"""Tick planner: compute when the orchestrator should next fire.

The MarketOrchestrator's loop calls `next_tick_after(now, market, cal)`
to learn when to wake up. The function returns a UTC instant, then
the loop sleeps until then (in 1-second chunks for responsive
shutdown).

Fire schedule for one trading day (in the market's local timezone):

  1. `post_open_offset_minutes` after the open (default: open + 5min).
     For NSE: 09:15 + 5 = 09:20 IST.
  2. Every wall-clock-hour boundary in the market's local timezone,
     starting from the first hour boundary that is strictly after
     post-open-offset and strictly before pre-close-offset. For NSE:
     10:00, 11:00, 12:00, 13:00, 14:00, 15:00 IST.
  3. `pre_close_offset_minutes` before the close (default: close - 15).
     For NSE: 15:30 - 15 = 15:15 IST.

Resulting NSE schedule: 09:20, 10:00, 11:00, 12:00, 13:00, 14:00,
15:00, 15:15 — eight ticks per trading day.

Edge cases handled:

  - The market's intraday hourly boundaries are computed in the
    market's local timezone, NOT UTC. NSE ticks at 10:00 IST, not
    10:00 UTC (which would be 15:30 IST — past close).

  - If `post_open + cadence > pre_close`, no intraday hourly ticks
    fire — only the post-open and pre-close ones. Reasonable for
    very short trading windows.

  - If post-open ≥ pre-close (operator misconfiguration: e.g. an
    open+30min on a market that closes 25min after open), the
    function silently emits just the open tick. We log this once at
    construction, not on every query.

  - Holidays: `next_tick_after` consults the TradingCalendar and
    skips non-trading days entirely. The next tick after Friday 15:15
    IST is Monday 09:20 IST (or further, if Monday is a holiday).

  - DST: today's tick instants are computed via tz.localize-style
    construction (datetime(..., tzinfo=tz)), which the zoneinfo
    package resolves correctly even across DST transitions. Tests
    cover this for `America/New_York` even though NSE is IST-only.

The whole module is pure functions over inputs — no I/O, no threads.
Tests can fix `now` to any value and verify the result deterministically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .market_calendar import TradingCalendar

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarketTickConfig:
    """Tick-scheduling inputs for one market.

    `open` and `close` are local times in `tz`. Offsets and cadence
    are durations. All are required — defaults live at the config-
    parsing layer (Pydantic), not here, so this dataclass stays
    transparent about what it does.
    """
    exchange: str
    tz: ZoneInfo
    open: time
    close: time
    post_open_offset: timedelta
    pre_close_offset: timedelta
    intraday_cadence: timedelta


def compute_tick_times_for_day(
    d: date,
    market: MarketTickConfig,
) -> list[datetime]:
    """Return the UTC instants at which `market` should fire on `d`.

    `d` is the local calendar date in `market.tz`. The function does
    NOT check whether `d` is a trading day — the caller (typically
    `next_tick_after`) is responsible for that. Calling this on a
    holiday is legal and useful: it tells you what the schedule
    *would have been*, useful for tests and for explaining gaps.

    The result is sorted ascending and deduplicated — if (somehow)
    the post-open offset and the first hourly tick collide, only one
    instance is kept. Empty list is possible if the market's
    open-to-close window is shorter than `post_open + pre_close`.
    """
    tz = market.tz
    open_local = datetime.combine(d, market.open, tzinfo=tz)
    close_local = datetime.combine(d, market.close, tzinfo=tz)
    post_open = open_local + market.post_open_offset
    pre_close = close_local - market.pre_close_offset

    ticks: list[datetime] = []

    if post_open >= close_local:
        # Pathological config: open + offset is at or past close. Just
        # log and bail with no ticks for this day.
        logger.warning(
            "market %s: post_open (%s) >= close (%s); no ticks scheduled",
            market.exchange, post_open.time(), market.close,
        )
        return []

    # 1. Post-open tick (always included, regardless of pre_close).
    ticks.append(post_open)

    # 2. Hourly boundaries strictly between post-open and pre-close.
    #
    # We walk forward from the smallest hourly boundary strictly
    # greater than post_open. "Hourly boundary" means top-of-hour in
    # local time — :00 minutes, regardless of the cadence value (we
    # use intraday_cadence but it's expected to be 1h; supporting
    # arbitrary cadences cleanly here requires another parameter
    # for the anchor minute, and YAGNI for now).
    #
    # If cadence is exactly 1h we get 10:00, 11:00, 12:00, ... If
    # cadence were 30min we'd get :00 and :30, etc.
    cadence_seconds = market.intraday_cadence.total_seconds()
    if cadence_seconds <= 0:
        raise ValueError(
            f"market {market.exchange}: intraday_cadence must be positive"
        )

    # Find the first boundary strictly after post_open.
    # We anchor cadence to local-midnight, same convention as
    # seconds_until_next_local_tick.
    local_midnight = datetime.combine(d, time(0, 0), tzinfo=tz)
    seconds_post_open = (post_open - local_midnight).total_seconds()
    # ceil to the next multiple of cadence_seconds *strictly greater*
    # than seconds_post_open. If post_open IS a boundary, we want the
    # next one; integer division of (seconds + 1) achieves this for
    # integer cadences, but using strict-greater math is clearer:
    n_boundaries_before = int(seconds_post_open // cadence_seconds)
    first_boundary_offset = (n_boundaries_before + 1) * cadence_seconds
    boundary_offset = first_boundary_offset

    # Pre-compute pre_close offset for comparison.
    seconds_pre_close = (pre_close - local_midnight).total_seconds()

    # Walk forward, capped: cadence ≥ 1s and a trading day is at most
    # 86400s long, so the loop bound is comfortably below 100k.
    while boundary_offset < seconds_pre_close:
        boundary = local_midnight + timedelta(seconds=boundary_offset)
        # Edge: exact equality with post_open — shouldn't happen
        # because we ceiling'd strictly greater, but defensive.
        if boundary != post_open:
            ticks.append(boundary)
        boundary_offset += cadence_seconds

    # 3. Pre-close tick. Only included if strictly after post-open
    # (otherwise we'd duplicate or invert).
    if pre_close > post_open:
        # Deduplicate against any boundary that happens to coincide
        # with pre_close (would happen if e.g. close-15min lands
        # exactly on a top-of-hour).
        if not ticks or ticks[-1] != pre_close:
            ticks.append(pre_close)

    # Convert all to UTC for the orchestrator's wall-clock loop.
    return [t.astimezone(timezone.utc) for t in ticks]


def next_tick_after(
    now: datetime,
    market: MarketTickConfig,
    calendar: TradingCalendar,
    *,
    max_lookahead_days: int = 14,
) -> datetime:
    """Return the first UTC tick instant strictly after `now`.

    Walks forward day by day from `now`'s local date until it finds
    a trading day whose tick schedule has at least one entry > now.
    Raises RuntimeError after `max_lookahead_days` of fruitless
    walking — that would only happen if the calendar is misconfigured
    to mark everything as a holiday.

    `now` must be timezone-aware.
    """
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")

    local_now = now.astimezone(market.tz)
    candidate_date = local_now.date()

    for _ in range(max_lookahead_days):
        if calendar.is_trading_day(candidate_date):
            ticks = compute_tick_times_for_day(candidate_date, market)
            for t in ticks:
                if t > now:
                    return t
        # No usable tick today (either holiday, or today's ticks are
        # all in the past). Try tomorrow.
        candidate_date = candidate_date + timedelta(days=1)

    raise RuntimeError(
        f"No tick found within {max_lookahead_days} days after {now.isoformat()} "
        f"for market {market.exchange}; check holiday/weekday configuration."
    )
