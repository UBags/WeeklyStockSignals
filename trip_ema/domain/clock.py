"""Injectable clocks.

Production code that needs to know "what time is it" takes a clock as an
argument instead of reaching for `time` / `datetime` at the point of use.
That makes elapsed-time behaviour (circuit-breaker reset timeouts, retry
backoff, staleness windows) unit-testable without sleeping and without
patching module globals.

Two aliases, deliberately distinct:

    MonotonicClock  -> float, seconds from an arbitrary origin. Never runs
                       backwards across NTP adjustments. Use for ELAPSED-TIME
                       comparisons (timeouts, backoff). Meaningless as an
                       absolute time.

    WallClock       -> tz-aware datetime in UTC. Use when the value is
                       persisted, logged, or compared against market dates.
                       NOT safe for elapsed-time maths.

Both are plain callables, so a test double is a lambda or a small class with
`__call__`. There is no Clock ABC and there should not be one.

Defaults live here so that call sites read `clock: MonotonicClock = monotonic_clock`
and nothing else has to import `time`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

MonotonicClock = Callable[[], float]
"""() -> seconds. Elapsed-time only. See module docstring."""

WallClock = Callable[[], datetime]
"""() -> tz-aware UTC datetime. Absolute time only. See module docstring."""


def monotonic_clock() -> float:
    """Default MonotonicClock. Wraps `time.monotonic()`."""
    return time.monotonic()


def system_utc_clock() -> datetime:
    """Default WallClock. Always tz-aware, always UTC.

    Callers that need IST convert at the edge (see domain.market_session);
    storing or comparing naive datetimes is a correctness bug in this codebase.
    """
    return datetime.now(UTC)
