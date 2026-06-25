"""Tick planner: compute when the orchestrator should next fire.

The MarketOrchestrator's loop calls `next_tick_after(now, market, cal)`
to learn when to wake up. The function returns a UTC instant, then
the loop sleeps until then (in 1-second chunks for responsive
shutdown).

There are two scheduling modes, selected by whether
`market.tick_schedule` is populated:

# Mode A — Phase 16a explicit tick schedule (new, four-instant)

When `market.tick_schedule` is non-None, the planner resolves the
configured `ResolvedTickEntry` list into the day's UTC instants:

  - `anchor: open`     → `open_local + offset_minutes`
  - `anchor: close`    → `close_local + offset_minutes`  (negative = before)
  - `anchor: midpoint` → arithmetic midpoint of the latest open-anchored
                         entry and the earliest close-anchored entry,
                         rounded to the nearest minute (half-up at :30s)

NSE example (open 09:15, close 15:30, IST) with the canonical schedule
`[post_open +15, mid_session midpoint, pre_close -30, post_close +60]`:

    09:30, 12:15, 15:00, 16:30 IST  →  four ticks per trading day.

NYSE example (open 09:30, close 16:00, ET) — same canonical schedule:

    09:45, 12:38, 15:30, 17:00 ET  (midpoint 12:37:30 rounds to 12:38).

A `close_override` parameter on `compute_tick_times_for_day` slides every
close-anchored instant (and therefore the midpoint) for short sessions
such as NYSE 1pm ET early-close days. The half-day detection itself —
discovering that a given date IS an early close — is not Step 16b's
concern; that wiring lands in a follow-up where the calendar/YAML
provides a `close_override_for_date(d)` hook.

# Mode B — Legacy hourly cadence (pre-Phase-16a behavior)

When `market.tick_schedule` is None, the planner emits the pre-Phase-16a
schedule unchanged:

  1. `post_open_offset` after the open (default: open + 5min).
     For NSE: 09:15 + 5 = 09:20 IST.
  2. Every wall-clock-hour boundary in the market's local timezone,
     strictly after post-open and strictly before pre-close.
     For NSE: 10:00, 11:00, 12:00, 13:00, 14:00, 15:00 IST.
  3. `pre_close_offset` before the close (default: close - 15).
     For NSE: 15:30 - 15 = 15:15 IST.

Resulting NSE legacy schedule: eight ticks per trading day.

# Edge cases (apply to both modes unless noted)

  - The market's intraday hourly boundaries are computed in the
    market's local timezone, NOT UTC. NSE ticks at 10:00 IST, not
    10:00 UTC (which would be 15:30 IST — past close).

  - If `post_open + cadence > pre_close` (legacy mode), no intraday
    hourly ticks fire — only the post-open and pre-close ones.

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

  - `close_override` is honored only in Mode A (tick_schedule). Passing
    it in Mode B is logged as a warning and ignored, since the legacy
    hourly cadence has no semantic for shrinking the window.

The whole module is pure functions over inputs — no I/O, no threads.
Tests can fix `now` to any value and verify the result deterministically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, UTC
from zoneinfo import ZoneInfo

from .market_calendar import TradingCalendar

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedTickEntry:
    """One resolved tick entry from the Phase 16a `tick_schedule:` block.

    Mirrors `trip_ema.config.schema.TickScheduleEntry` but lives in the
    scheduler layer so callers don't drag the Pydantic model into pure
    scheduling code. Constructed from the YAML model via
    `MarketHoursConfig.to_tick_config()`.

    `anchor` is one of: 'open', 'close', 'midpoint'. The schema layer
    enforces this — we don't re-validate at runtime.
    """
    name: str
    anchor: str
    offset_minutes: int


@dataclass(frozen=True, slots=True)
class MarketTickConfig:
    """Tick-scheduling inputs for one market.

    `open` and `close` are local times in `tz`. Legacy offsets and
    cadence are durations. `tick_schedule`, if present, REPLACES the
    legacy offsets/cadence — see the module docstring's Mode A.

    All legacy fields are required for backward compatibility with
    callers that build MarketTickConfig directly. New callers using
    the Phase 16a path can leave them at any sensible value; they
    are ignored when tick_schedule is non-None.
    """
    exchange: str
    tz: ZoneInfo
    open: time
    close: time
    post_open_offset: timedelta
    pre_close_offset: timedelta
    intraday_cadence: timedelta
    # Phase 16a addition. None means "use legacy hourly cadence".
    tick_schedule: tuple[ResolvedTickEntry, ...] | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_tick_times_for_day(
    d: date,
    market: MarketTickConfig,
    *,
    close_override: time | None = None,
) -> list[datetime]:
    """Return the UTC instants at which `market` should fire on `d`.

    `d` is the local calendar date in `market.tz`. The function does
    NOT check whether `d` is a trading day — the caller (typically
    `next_tick_after`) is responsible for that. Calling this on a
    holiday is legal and useful: it tells you what the schedule
    *would have been*, useful for tests and for explaining gaps.

    `close_override` (Phase 16a, Mode A only): if supplied, this local
    time replaces `market.close` for the purposes of this day's
    close-anchored instants. Use for NYSE 1pm ET early-close days
    and similar shortened sessions. Has no effect in Mode B (legacy),
    where it is logged as a warning and ignored.

    The result is sorted ascending and deduplicated. Empty list is
    possible if the market's window is shorter than `post_open +
    pre_close`.
    """
    if market.tick_schedule is not None:
        return _compute_from_tick_schedule(
            d, market, close_override=close_override,
        )
    if close_override is not None:
        # Mode B has no semantic for shrinking the session; warn so
        # operators don't silently lose half-day handling on a
        # market still on the legacy schedule.
        logger.warning(
            "market %s: close_override=%s ignored — legacy cadence does not "
            "support per-day close changes. Migrate to tick_schedule.",
            market.exchange, close_override,
        )
    return _compute_legacy(d, market)


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

    Note (Phase 16a): this function calls `compute_tick_times_for_day`
    without a `close_override`. Half-day support requires a follow-up
    that lets the caller inject per-date close overrides (typically
    sourced from the holidays YAML's `early_close_days` block). Until
    then, on early-close days the planner will compute close-anchored
    instants relative to the regular close.
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


# ---------------------------------------------------------------------------
# Mode A — tick_schedule resolution (Phase 16a)
# ---------------------------------------------------------------------------


def _compute_from_tick_schedule(
    d: date,
    market: MarketTickConfig,
    *,
    close_override: time | None,
) -> list[datetime]:
    """Resolve a configured tick_schedule into UTC instants for `d`.

    Two-phase resolution:
      Phase 1: walk the entries; for each `open`/`close`-anchored entry,
               compute its absolute local instant.
      Phase 2: resolve midpoint entries from the flanking open/close
               instants (schema validation guarantees both exist).

    Midpoint timing is rounded to the nearest minute (half-up at :30s)
    so logs and emails carry whole-minute values instead of the raw
    midpoint's :30 seconds (NYSE 12:37:30 → 12:38).
    """
    schedule = market.tick_schedule
    assert schedule is not None, "dispatcher precondition"

    tz = market.tz
    open_local = datetime.combine(d, market.open, tzinfo=tz)
    effective_close = close_override if close_override is not None else market.close
    close_local = datetime.combine(d, effective_close, tzinfo=tz)

    # Phase 1: resolve open- and close-anchored entries.
    by_name: dict[str, datetime] = {}
    open_anchored: list[datetime] = []
    close_anchored: list[datetime] = []
    midpoint_names: list[str] = []

    for entry in schedule:
        if entry.anchor == "open":
            instant = open_local + timedelta(minutes=entry.offset_minutes)
            by_name[entry.name] = instant
            open_anchored.append(instant)
        elif entry.anchor == "close":
            instant = close_local + timedelta(minutes=entry.offset_minutes)
            by_name[entry.name] = instant
            close_anchored.append(instant)
        elif entry.anchor == "midpoint":
            midpoint_names.append(entry.name)
        else:
            # Schema validation makes this unreachable; defensive.
            raise ValueError(
                f"market {market.exchange}: tick {entry.name!r} has unknown "
                f"anchor {entry.anchor!r}; expected one of 'open', 'close', "
                f"'midpoint'."
            )

    # Phase 2: resolve midpoint(s).
    # Schema's _validate_tick_schedule guarantees that if any midpoint
    # exists, at least one open- AND one close-anchored entry also exist,
    # so the max()/min() calls below are safe.
    if midpoint_names:
        latest_open = max(open_anchored)
        earliest_close = min(close_anchored)
        midpoint_raw = latest_open + (earliest_close - latest_open) / 2
        midpoint_rounded = _round_to_nearest_minute(midpoint_raw)
        for name in midpoint_names:
            by_name[name] = midpoint_rounded

    # Convert to UTC, dedupe (a collision between two distinct named
    # ticks would be unusual but is silently handled the same way the
    # legacy path handles it — one fire per instant). Sort ascending
    # for the orchestrator's consumption.
    utc_instants = sorted(
        {inst.astimezone(UTC) for inst in by_name.values()}
    )
    return utc_instants


def _round_to_nearest_minute(dt: datetime) -> datetime:
    """Round `dt` to the nearest whole minute. Half-up at :30 seconds.

    Used to land midpoint instants on whole-minute boundaries so logs
    and emails carry clean times. NYSE midpoint 12:37:30 → 12:38; NSE
    midpoint 12:15:00 → 12:15 (no change).

    The half-up rule is deliberate (not Python's default banker's
    rounding via `round()`): at exactly :30s we round up, period. That
    matches operator expectation ("12:37:30 reads as closer to 12:38")
    and is independent of whether the surrounding minute is odd/even.
    """
    seconds_in_minute = dt.second + dt.microsecond / 1_000_000
    truncated = dt.replace(second=0, microsecond=0)
    if seconds_in_minute >= 30.0:
        return truncated + timedelta(minutes=1)
    return truncated


# ---------------------------------------------------------------------------
# Mode B — legacy hourly cadence (pre-Phase-16a)
# ---------------------------------------------------------------------------


def _compute_legacy(d: date, market: MarketTickConfig) -> list[datetime]:
    """Compute the legacy hourly schedule.

    Body lifted verbatim from the pre-Phase-16a `compute_tick_times_for_day`.
    Kept here behind the dispatcher so backward-compat for callers using
    a MarketTickConfig with `tick_schedule=None` is bit-for-bit identical.
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
    return [t.astimezone(UTC) for t in ticks]