"""Rate-limit state for a yfinance fetch pass.

Yahoo does not raise when it throttles. It returns the frame with every
requested symbol's column present and all-NaN, which normalises into a
FetchOutcome with no bars and no error — a "success" containing nothing. The
only way to tell that apart from a genuinely dataless ticker is corroboration.

The old rule lived inside a single batch call and read: if EVERY symbol in this
frame came back empty, and there are at least two of them, it is a throttle.
Sound, but it has a hole exactly where it matters. The recovery path re-fetches
flaked tickers ONE AT A TIME (batch_size=1), and a lone empty frame is
genuinely ambiguous — so the rule could never fire there. The result: when
Yahoo was throttling the whole window, the solo passes walked the entire
snapshot at full speed against a closed door, got nothing, and reported every
ticker as dataless. The backoff existed; it simply could not be reached from
the path that needed it.

The fix is to notice that the corroboration does not have to come from one
frame. N consecutive single-symbol frames, all empty, is the same evidence as
one N-symbol frame that is entirely empty — spread over time instead of across
columns. So the state moves OUT of the batch call and onto an object the whole
pass shares:

    ThrottleGate — one per fetch pass, shared by every YFinanceSource in it.

      observe_empty(n)  a frame came back with no bars for any of its n symbols.
                        n >= 2  -> unanimity; trip immediately (the old rule).
                        n == 1  -> a data point. Trip once the streak reaches
                                   solo_empty_streak_to_trip.
      observe_data()    bars arrived. Yahoo is answering. Reset everything.
      observe_rate_limit_error(e)
                        an explicit 429 / "too many requests". Trip, and use the
                        long backoff rather than the 2s/4s exception one — a
                        rate-limit is not a flake and retrying in two seconds is
                        just another swing at the same closed door.
      wait()            sleep out whatever is left of an open backoff window.
                        Called before every fetch, so once the gate trips the
                        entire pass slows to the backoff rate instead of
                        hammering.

Backoff is exponential from base_seconds, capped at max_seconds, and jittered
so parallel workers do not resynchronise on the same retry instant.

The clock, the sleep, and the jitter are all injected. This whole module is
about elapsed time, and a test that had to actually wait 30 seconds to check a
30-second backoff would not be written.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..domain.clock import MonotonicClock, monotonic_clock
from .ports import Sleeper

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThrottleConfig:
    """Tunables. Conservative by default: a fetch pass that backs off too much
    is slow, and a fetch pass that backs off too little gets nothing at all and
    writes a run's worth of tickers off as dataless. Wrong data is the
    expensive failure; slow is not."""

    base_seconds: float = 30.0
    """First backoff. Doubles on each subsequent trip."""

    max_seconds: float = 300.0
    """Ceiling on the un-jittered backoff."""

    solo_empty_streak_to_trip: int = 3
    """How many consecutive single-symbol empty frames before we call it a
    throttle. One is ambiguous (delisted, never traded, rights entitlement).
    Three in a row, with no data in between, is not a coincidence."""

    max_backoffs: int = 4
    """After this many trips without recovery, stop backing off and let the
    outcomes surface as empty. Yahoo is not going to answer today, and the
    caller's own bookkeeping (empty_tickers, the park threshold) takes it from
    here. Bounded, because an unbounded backoff is an unbounded hang."""

    trip_on_empty: bool = True
    """When False, observe_empty() is a no-op — the retry cohort and the
    reference sweeps disable this because empties in those passes are expected
    by construction (delisted, genuinely dataless, no financials to report).
    Only observe_rate_limit_error() (explicit 429) can trip the gate in that
    mode. See D2 in fetch_routine_fixes.md."""


def looks_rate_limited(e: BaseException) -> bool:
    """True when an exception is Yahoo saying "too many requests".

    Message-substring matching, for the same reason the fundamentals fetchers
    do it: yfinance's exception hierarchy is not stable across versions, and a
    429 arrives wearing whatever type the HTTP layer of the day happened to
    raise."""
    msg = str(e).lower()
    if "429" in msg:
        return True
    return ("rate" in msg and "limit" in msg) or "too many requests" in msg


class ThrottleGate:
    """Shared rate-limit state for one fetch pass. Not thread-safe by design:
    one gate per pass, and the passes that share a gate are sequential. The
    batched pass fans out across threads, but each thread builds its own
    source, and a per-thread gate is the correct scope there — a throttle
    observed on one thread's batch says nothing the other threads' own frames
    will not say a moment later."""

    def __init__(
        self,
        config: ThrottleConfig | None = None,
        *,
        sleep: Sleeper = time.sleep,
        clock: MonotonicClock = monotonic_clock,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._config = config or ThrottleConfig()
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._level = 0
        self._solo_empty_streak = 0
        self._open_until: float | None = None

    # ---------- observation ---------- #

    def observe_data(self) -> None:
        """Bars arrived. Yahoo is answering; forget everything."""
        self._level = 0
        self._solo_empty_streak = 0
        self._open_until = None

    def observe_empty(self, n_symbols: int) -> bool:
        """A frame came back with no bars for any of its `n_symbols`.

        Returns True if the caller should re-fetch this batch (a backoff window
        is now open and `wait()` will sit it out). False means "take the empty
        outcomes at face value" — either not enough evidence yet, or we have
        already backed off max_backoffs times and Yahoo is simply not answering.
        """
        if not self._config.trip_on_empty:
            # D2: retry cohort / reference sweeps disable this — empties are
            # expected by construction. Only explicit 429s trip the gate.
            return False
        if n_symbols >= 2:
            # Unanimity within one frame. One equity returning zero bars over a
            # multi-year window is plausible; all of them at once is not a mass
            # delisting.
            self._solo_empty_streak = 0
            return self._trip(f"all {n_symbols} symbols in the batch returned zero bars")

        self._solo_empty_streak += 1
        if self._solo_empty_streak < self._config.solo_empty_streak_to_trip:
            return False
        return self._trip(
            f"{self._solo_empty_streak} consecutive solo fetches returned zero bars"
        )

    def observe_rate_limit_error(self, e: BaseException) -> bool:
        """An explicit 429. Trip on the long backoff, not the flake backoff."""
        self._solo_empty_streak = 0
        return self._trip(f"rate-limit error from yfinance: {e}")

    # ---------- backoff ---------- #

    def wait(self) -> float:
        """Sleep out whatever is left of the open backoff window. Returns the
        seconds slept (0.0 if the gate is closed). Call before every fetch."""
        if self._open_until is None:
            return 0.0
        remaining = self._open_until - self._clock()
        if remaining <= 0:
            self._open_until = None
            return 0.0
        self._sleep(remaining)
        self._open_until = None
        return remaining

    def _trip(self, reason: str) -> bool:
        if self._level >= self._config.max_backoffs:
            logger.warning(
                "yfinance throttle: %s, but %d backoffs already spent — "
                "giving up and letting the empty outcomes stand",
                reason, self._level,
                extra={"backoff_level": self._level},
            )
            return False
        self._level += 1
        delay = min(
            self._config.base_seconds * (2 ** (self._level - 1)),
            self._config.max_seconds,
        )
        delay *= 0.5 + self._jitter()  # don't resynchronise parallel workers
        self._open_until = self._clock() + delay
        logger.warning(
            "yfinance THROTTLED (%s); backing off %.1fs (level %d/%d)",
            reason, delay, self._level, self._config.max_backoffs,
            extra={"backoff_seconds": delay, "backoff_level": self._level},
        )
        return True

    # ---------- introspection ---------- #

    def snapshot(self) -> dict:
        """For logs and tests. Not a control surface."""
        return {
            "backoff_level": self._level,
            "solo_empty_streak": self._solo_empty_streak,
            "backoff_open": self._open_until is not None,
        }
