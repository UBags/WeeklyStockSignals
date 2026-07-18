"""Process-wide minimum spacing between outbound provider requests.

Shared by the fundamentals reference sweeps (one HTTP call per ticker) and
the OHLCV bar fetch (one call per batch). Both hit the same Yahoo per-IP
limiter from the same box, and both used to pace themselves PER THREAD --
which does nothing about the burst that actually trips the limiter, because
W workers can still fire W requests within milliseconds of one another.

Threads reserve their slot under a lock and sleep OUTSIDE it, so the HTTP
calls themselves still overlap -- only their START TIMES are spaced. Uses
time.monotonic(), so a wall-clock/NTP step cannot collapse the spacing.

THROUGHPUT IS THEN THE GAP ALONE: ~1/gap requests per second, and extra
threads no longer add rate (they only hide slow responses).

gap <= 0 makes wait() a no-op, restoring ungated behaviour exactly.

`clock` and `sleep` are injected so tests can drive the gate deterministically
without patching the time module.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RequestGate:
    """Minimum wall-clock spacing between request START times."""

    __slots__ = ("_clock", "_gap", "_lock", "_next_at", "_sleep")

    def __init__(
        self,
        gap_s: float = 0.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._gap = max(0.0, gap_s)
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._clock = clock
        self._sleep = sleep

    @property
    def gap_seconds(self) -> float:
        return self._gap

    def wait(self) -> float:
        """Block until this caller's slot. Returns seconds actually slept."""
        if self._gap <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            start_at = now if now > self._next_at else self._next_at
            self._next_at = start_at + self._gap
        delay = start_at - now
        if delay > 0:
            self._sleep(delay)
            return delay
        return 0.0
