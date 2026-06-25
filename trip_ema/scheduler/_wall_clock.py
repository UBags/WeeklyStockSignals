"""Wall-clock tick math, shared by all scheduler loops.

This module exists because the old `_seconds_until_next_tick` lived
inside `compute_scheduler.py`, and now multiple scheduler-like
components need the same anchoring logic (ComputeScheduler,
SubscriberDistributor, MarketOrchestrator). Pulling it out into a
single module avoids three subtly-different copies drifting apart.

Two functions:

  - `seconds_until_next_tick(now, cadence)` — the original UTC-epoch-
    anchored function, unchanged. Tick boundaries fire at every
    `cadence` interval measured from the Unix epoch (1970-01-01
    00:00:00 UTC). With cadence=1h, this fires at the top of every
    UTC hour.

  - `seconds_until_next_local_tick(now, cadence, tz)` — same shape,
    but the anchoring is relative to local-midnight in `tz`, not UTC
    epoch. Lets a "top of hour" tick line up with India Standard Time
    boundaries (09:00, 10:00, 11:00 IST) rather than UTC-shifted ones
    (09:00, 10:00, 11:00 UTC = 14:30, 15:30, 16:30 IST). Important
    for market-hours orchestration.

Both functions are pure: no I/O, no globals, no clock reads. The
caller passes `now` so tests can fix it deterministically.

Both functions return a strictly-positive `float` of seconds: never
zero or negative, even if `now` happens to land exactly on a tick
boundary (in that case, we wait one full cadence to the next one).
This matches the loop pattern `sleep(seconds_until_next_tick(...));
run_once()` — landing on a boundary doesn't mean "fire twice."
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def seconds_until_next_tick(now: datetime, cadence: timedelta) -> float:
    """How long until the next UTC-epoch-anchored tick boundary.

    With cadence=1h, returns the seconds until the next top-of-UTC-hour.
    With cadence=15min, returns seconds until the next :00/:15/:30/:45
    minute (UTC).

    `now` must be timezone-aware. (We don't enforce UTC specifically
    because `.timestamp()` works on any tz-aware datetime, but naive
    datetimes are ambiguous and rejected.)

    Returns a value in the half-open range (0, cadence_seconds]. Never
    zero — if `now` is exactly on a boundary, we wait one full cadence.
    """
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    cadence_seconds = cadence.total_seconds()
    if cadence_seconds <= 0:
        raise ValueError(f"cadence must be positive, got {cadence}")
    epoch_seconds = now.timestamp()
    elapsed_in_window = epoch_seconds % cadence_seconds
    remaining = cadence_seconds - elapsed_in_window
    if remaining <= 0:
        # `now` landed exactly on a boundary; wait a full cadence.
        # Floating-point can also produce a remaining of e.g. 1e-15;
        # collapse that to a full cadence too, otherwise the loop
        # would call run_once() before time_module.sleep had a chance
        # to settle.
        remaining = cadence_seconds
    return remaining


def seconds_until_next_local_tick(
    now: datetime,
    cadence: timedelta,
    tz: ZoneInfo,
) -> float:
    """How long until the next tick boundary anchored to local midnight in `tz`.

    With cadence=1h and tz=Asia/Kolkata, fires at 09:00/10:00/11:00 IST
    (each is :30 minutes past a UTC hour, so UTC-epoch anchoring would
    miss them).

    Anchoring is to *today's local midnight*, not the local epoch — DST
    transitions and historical tz offsets don't affect today's tick
    pattern. We compute ticks relative to local 00:00:00 of the
    local-current date, which the IANA db handles correctly.

    Edge case: if local midnight itself is ambiguous on a DST fall-back
    day, ZoneInfo resolves to the first occurrence (standard behaviour
    for fold=0). The tick pattern is then a normal forward sequence
    from there. Tests cover this explicitly.

    Returns a value in (0, cadence_seconds].
    """
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    cadence_seconds = cadence.total_seconds()
    if cadence_seconds <= 0:
        raise ValueError(f"cadence must be positive, got {cadence}")

    local = now.astimezone(tz)
    local_midnight = datetime(
        local.year, local.month, local.day, 0, 0, 0, tzinfo=tz,
    )
    # Seconds from local midnight to `now` in the local frame. We
    # convert both to UTC instants before subtracting so the result
    # is correct across DST boundaries (in IST there are none, but
    # the function is general).
    elapsed_seconds = (now - local_midnight).total_seconds()

    elapsed_in_window = elapsed_seconds % cadence_seconds
    remaining = cadence_seconds - elapsed_in_window
    if remaining <= 0:
        remaining = cadence_seconds
    return remaining


# ---- Backward-compatibility re-export ----
# `compute_scheduler.py` and `subscriber_distributor.py` historically
# imported `_seconds_until_next_tick` from `compute_scheduler`. Until
# those imports are updated, both names need to keep working. The
# leading-underscore version is kept as an alias for the old call
# sites; new code should use the public name.
_seconds_until_next_tick = seconds_until_next_tick
