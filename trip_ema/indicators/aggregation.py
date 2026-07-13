"""Bar aggregation across timeframes.

The Pine indicator is timeframe-agnostic — it just processes whatever bars
it's given. The Python port stores OHLCV at the *native* fetch resolution
(15m, hourly, 2-hourly, daily) and aggregates to weekly internally.

Week anchor: each exchange's local Monday 00:00. Equity markets in
different geographies anchor their trading week differently — NSE's
Monday starts at IST 00:00 (UTC 18:30 the prior day), NYSE's at ET 00:00
(UTC 05:00), LSE's at GMT 00:00. The `tz` arg to _monday_of() resolves
this consistently.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, UTC
from collections.abc import Iterable
from zoneinfo import ZoneInfo

from ..domain import OHLCVBar, Timeframe
from ..domain.market_session import monday_instant_of


def _monday_of(d: datetime, tz: str = "UTC") -> datetime:
    """Return the Monday 00:00 of the *local* week containing `d`,
    represented as a UTC-aware datetime.

    The week is anchored in the supplied timezone — for `tz='Asia/Kolkata'`,
    that's IST Monday 00:00, which corresponds to Sunday 18:30 UTC. The
    return value is always converted to UTC for storage consistency.

    The input `d` must be tz-aware. If it isn't, raises ValueError.

    L1-ANCHOR-RULE: the arithmetic now lives in domain.market_session, which is
    already the declared authority on exchange time. This is the tz-STRING
    spelling of the same fact (aggregation is handed a zone name, not an
    exchange); market_session.week_anchor is the exchange spelling. One
    implementation, two front doors — not two implementations.
    """
    return monday_instant_of(d, ZoneInfo(tz))


def bucket_end(
    bar_timestamp: datetime,
    timeframe: Timeframe,
    tz: str = "UTC",
) -> datetime:
    """Return the exclusive upper bound of the bucket that `bar_timestamp`
    represents, in the supplied timezone.

    For WEEKLY in `tz='Asia/Kolkata'`: bar_timestamp is IST-Monday 00:00
    (stored as UTC); bucket ends at next IST-Monday 00:00 (UTC-formatted).
    For DAILY: bar_timestamp is local-midnight; bucket ends at next
    local-midnight, both UTC-formatted.

    Used by the ComputeScheduler to determine whether a bar should be
    treated as provisional (bucket still open) or final.

    DST correctness: in zones that observe DST (America/New_York,
    Europe/London, etc.), a local week is 167 or 169 hours during
    transition weeks — not always exactly 168. Likewise a local day is
    23 or 25 hours on transition days, not 24. Adding `timedelta(days=7)`
    or `timedelta(days=1)` to a tz-aware datetime adds wall-clock UTC
    elapsed time, which crosses DST boundaries incorrectly. To get
    correct bucket boundaries we must do calendar arithmetic on the
    naive *date* in the local zone, then combine with local midnight,
    then convert to UTC. That way ZoneInfo applies the correct local
    offset for the target date.

    For UTC and other non-DST zones (Asia/Kolkata, Asia/Tokyo, etc.),
    this returns the same result as timedelta arithmetic, so it's a
    no-op for the primary market the system serves.
    """
    if timeframe == Timeframe.WEEKLY:
        # Compute the next Monday's *date* in the local zone, then
        # combine with local midnight. This handles DST correctly: if
        # the week contains a spring-forward, the next Monday is 167
        # hours after the prior Monday; if fall-back, 169 hours.
        zone = ZoneInfo(tz)
        local = bar_timestamp.astimezone(zone)
        monday_local_date = local.date() - timedelta(days=local.weekday())
        next_monday_local_date = monday_local_date + timedelta(days=7)
        next_monday_local = datetime.combine(
            next_monday_local_date, time.min, tzinfo=zone,
        )
        return next_monday_local.astimezone(UTC)
    if timeframe == Timeframe.DAILY:
        zone = ZoneInfo(tz)
        local = bar_timestamp.astimezone(zone)
        # Add one CALENDAR day, then re-combine with local midnight. This
        # gives the correct UTC instant of next-local-midnight even
        # across DST transitions.
        next_day_local_date = local.date() + timedelta(days=1)
        next_midnight_local = datetime.combine(
            next_day_local_date, time.min, tzinfo=zone,
        )
        return next_midnight_local.astimezone(UTC)
    raise ValueError(f"bucket_end not defined for {timeframe}")


def is_provisional(
    bar_timestamp: datetime,
    timeframe: Timeframe,
    now: datetime,
    tz: str = "UTC",
) -> bool:
    """True if the bar's bucket is still open as of `now` — i.e., new
    OHLCV ticks could still arrive that revise this bar.

    `now` is interpreted in `tz`'s local sense via bucket_end; that is,
    a weekly bar at IST-Monday is "still open" up to but not including
    next IST-Monday 00:00.
    """
    return now < bucket_end(bar_timestamp, timeframe, tz=tz)


def recommended_min_bars_per_week(source_tf: Timeframe) -> int:
    """Heuristic threshold for 'this week's source bar count looks
    complete' as a function of the source timeframe.

    Used by `daily_to_weekly`'s `min_bars_for_complete_week` parameter.
    Numbers are deliberately conservative — we'd rather miss a complete
    week than emit a partial one, since the engine's provisional path
    will pick up the genuine-complete week on the next cycle.

    Calibration: assumes a typical equity-trading week of 5 days × 6.5
    trading hours. Tweak per exchange if the session is materially
    different.
    """
    return {
        Timeframe.MIN_15:  80,   # ~130 expected; allow 80
        Timeframe.HOURLY:  20,   # ~32 expected; allow 20
        Timeframe.HOURLY_2: 10,  # ~16 expected; allow 10
        Timeframe.DAILY:    5,   # 5 expected; 5 is the right floor
        Timeframe.WEEKLY:   1,   # trivially complete with itself
    }[source_tf]


def daily_to_weekly(
    daily_bars: Iterable[OHLCVBar],
    *,
    include_partial_last_week: bool = True,
    tz: str = "UTC",
    min_bars_for_complete_week: int | None = None,
) -> list[OHLCVBar]:
    """Aggregate finer-resolution bars into weekly bars, anchored at
    the supplied timezone's local Monday 00:00.

    Despite the function name, this accepts ANY finer timeframe: 15-min,
    hourly, 2-hourly, daily. The week-boundary detection just looks at
    each bar's `_monday_of(timestamp, tz)`; bars in the same week
    aggregate together regardless of how many there are.

    Args:
        daily_bars: Source OHLCVBars, sorted by timestamp ascending.
            Out-of-order input is not detected; caller's responsibility.
        include_partial_last_week: If True (default), the last week is
            included even if it's not yet complete. If False, only
            fully-closed weeks (the latest week whose bucket has ended)
            are returned.
        tz: The exchange's timezone — drives where the week boundaries
            land. Default 'UTC' for backward compat with old tests.
        min_bars_for_complete_week: When `include_partial_last_week=False`,
            the threshold above which the latest week is considered
            complete and emitted. Defaults to 5 (right for daily source,
            way too low for hourly). Callers with finer source timeframes
            should pass `recommended_min_bars_per_week(source_tf)`.

    Aggregation rules per week:
        open   = first bar's open
        high   = max of all bars' highs
        low    = min of all bars' lows
        close  = last bar's close
        volume = sum of all bars' volumes
    """
    bars_list = list(daily_bars)
    if not bars_list:
        return []

    weekly: list[OHLCVBar] = []
    current_monday: datetime | None = None
    week_open: float | None = None
    week_high: float = float("-inf")
    week_low: float = float("inf")
    week_close: float | None = None
    week_volume: float = 0.0
    week_bar_count: int = 0

    def _flush() -> None:
        nonlocal current_monday, week_open, week_high, week_low
        nonlocal week_close, week_volume, week_bar_count
        if current_monday is None or week_open is None or week_close is None:
            return
        weekly.append(OHLCVBar(
            timestamp=current_monday,
            open=week_open,
            high=week_high,
            low=week_low,
            close=week_close,
            volume=week_volume,
        ))
        current_monday = None
        week_open = None
        week_high = float("-inf")
        week_low = float("inf")
        week_close = None
        week_volume = 0.0
        week_bar_count = 0

    for bar in bars_list:
        monday = _monday_of(bar.timestamp, tz=tz)
        if current_monday is None:
            current_monday = monday
            week_open = bar.open
        elif monday != current_monday:
            _flush()
            current_monday = monday
            week_open = bar.open

        week_high = max(week_high, bar.high)
        week_low = min(week_low, bar.low)
        week_close = bar.close
        week_volume += bar.volume
        week_bar_count += 1

    # Final week — flush only if complete, or if partial-inclusion is allowed.
    # "Complete" is approximated by bar-count ≥ threshold. Defaults to 5,
    # which is right for daily source. Finer source timeframes (hourly,
    # 15-min) need a higher threshold; the caller passes it via
    # `min_bars_for_complete_week` derived from `recommended_min_bars_per_week`.
    threshold = min_bars_for_complete_week if min_bars_for_complete_week is not None else 5
    if current_monday is not None:
        is_complete = week_bar_count >= threshold
        if is_complete or include_partial_last_week:
            _flush()

    return weekly


# ============================================================
# A2 (Phase 9) — Gap detection
#
# Under the user's data-availability model, the ETL is the source of
# truth for "is there a bar for week T?". The application is a consumer:
# if expected source bars are missing, it refuses to advance the engine
# and flags the ticker as stale. The ETL is responsible for filling the
# gap; once filled, the next compute pass proceeds normally.
#
# The helpers below let the scheduler enumerate the weekly buckets that
# *should* have at least one source bar by now, and check that fact
# given a set of available source-bar timestamps.
# ============================================================


def expected_closed_weekly_buckets(
    engine_last_ts: datetime,
    now: datetime,
    tz: str = "UTC",
) -> list[datetime]:
    """List of weekly-Monday timestamps (UTC) that represent CLOSED weeks
    between `engine_last_ts` (exclusive) and `_monday_of(now)` (exclusive).

    These are the weeks for which the ETL should already have written
    source bars: each one is a fully-closed local week that lies after
    the engine's last committed bar and before the currently-open week.

    The currently-open week (the one containing `now`) is *not* in the
    expected list — it may legitimately be empty mid-week before any
    source bar has arrived yet.

    Empty list means "no closed buckets are expected" — either the
    engine is up-to-date (its provisional is for the current week) or
    `now` is in the same week as `engine_last_ts`. Either way, no gap
    detection applies.
    """
    if engine_last_ts.tzinfo is None or now.tzinfo is None:
        raise ValueError("expected_closed_weekly_buckets requires tz-aware datetimes")
    # Start from the week AFTER engine_last_ts's week. (engine_last_ts
    # is itself the Monday of a week the engine has already ingested —
    # potentially provisional. The first week we expect to find data
    # for is the one strictly after.)
    last_monday = _monday_of(engine_last_ts, tz=tz)
    current_monday = _monday_of(now, tz=tz)
    buckets: list[datetime] = []
    cursor = last_monday + timedelta(days=7)
    # Iterate by adding 7 days. This is safe within a non-DST zone (UTC
    # arithmetic) — but to be DST-correct in zones like America/New_York,
    # we use bucket_end's local-date arithmetic via _monday_of repeatedly.
    # bucket_end on a Monday gives next Monday; we use that to step.
    while cursor < current_monday:
        buckets.append(cursor)
        # Advance to the next local Monday (DST-correct)
        cursor = bucket_end(cursor, Timeframe.WEEKLY, tz=tz)
    return buckets


def detect_weekly_gaps(
    source_bar_timestamps: list[datetime],
    engine_last_ts: datetime,
    now: datetime,
    tz: str = "UTC",
) -> list[datetime]:
    """Return the list of expected CLOSED weekly buckets (UTC Monday
    timestamps) that have ZERO source bars in them.

    Args:
        source_bar_timestamps: All source bar timestamps within the
            time range [engine_last_ts, now]. Order doesn't matter.
        engine_last_ts: The engine's last committed bar timestamp.
        now: Current wall-clock time.
        tz: The ticker's local timezone (for week-anchor computation).

    Returns:
        Empty list = no gap, ETL is current.
        Non-empty = one or more closed weeks have no source bars.
            Each entry is the UTC timestamp of a missing week's local-
            Monday-midnight. The scheduler should NOT advance the engine
            for this ticker; instead, flag stale and wait for ETL.
    """
    expected = expected_closed_weekly_buckets(engine_last_ts, now, tz=tz)
    if not expected:
        return []
    # Bucket each source bar by its local Monday
    present_mondays: set[datetime] = set()
    for ts in source_bar_timestamps:
        present_mondays.add(_monday_of(ts, tz=tz))
    return [m for m in expected if m not in present_mondays]
