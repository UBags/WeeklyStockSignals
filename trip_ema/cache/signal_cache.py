"""SignalCache — read-mostly cache of the latest BarState per ticker.

The ComputeScheduler is the *only writer*. Readers (HTTP API, the
SubscriberDistributor) acquire no lock; they read whatever entry is
currently in the dict. Updates are atomic at the per-key level: a write
replaces an immutable CachedSignal with another immutable CachedSignal.

We use a single threading.Lock to serialize *writes* against each other —
not against reads — so two concurrent compute paths (shouldn't happen,
but defensive) can't lose updates.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from ..domain import BarState, ChangeSignal, TickerRef, Timeframe


CacheKey = tuple[TickerRef, Timeframe]


@dataclass(frozen=True, slots=True)
class CachedSignal:
    """The unit of cache content. Immutable — replacement, not mutation."""

    ticker: TickerRef
    timeframe: Timeframe
    latest_state: BarState
    latest_close: float                   # bar.close of the most recent ingested bar
    state_history: tuple[BarState, ...]   # most recent last
    last_change: ChangeSignal             # most recent CHANGE event (not NO_CHANGE)
    last_change_bar: datetime | None      # bar_timestamp of the last change event
    last_change_computed_at: datetime | None
    computed_at: datetime                 # when this snapshot was last written
    stale_since: datetime | None = None
    """A2 (Phase 9): when set, this entry represents the *last good* state
    before a data gap was detected. The ComputeScheduler refuses to
    advance the engine for this ticker until the gap is filled by the
    ETL. `stale_since` is the wall-clock timestamp of the first compute
    pass that observed the gap. Set to None whenever a compute pass
    proceeds normally — clearing the stale flag once data is fresh again.
    """


class SignalCache:
    """Read-mostly per-ticker cache. Thread-safe under the access pattern
    documented in the module docstring."""

    def __init__(self):
        self._entries: dict[CacheKey, CachedSignal] = {}
        self._write_lock = threading.Lock()

    # ---------- Reads (lock-free) ---------- #

    def get(self, ticker: TickerRef, timeframe: Timeframe) -> CachedSignal | None:
        """O(1) lookup. Returns the immutable snapshot or None.
        Lock-free — relies on the GIL for atomicity of the dict lookup."""
        return self._entries.get((ticker, timeframe))

    def has(self, ticker: TickerRef, timeframe: Timeframe) -> bool:
        return (ticker, timeframe) in self._entries

    def all_signals(self) -> list[CachedSignal]:
        """Snapshot of every cached signal. Iteration is safe because dict
        copy is atomic under the GIL."""
        return list(self._entries.values())

    def stale_signals(self) -> list[CachedSignal]:
        """A2 (Phase 9): every cached signal whose `stale_since` is set
        (the ticker has a data gap and the scheduler is refusing to
        advance until the ETL backfills). Used by `/readyz` and
        `/v1/admin/stale-tickers`."""
        return [s for s in self._entries.values() if s.stale_since is not None]

    def __len__(self) -> int:
        return len(self._entries)

    # ---------- Writes (single-writer-friendly) ---------- #

    def put(self, signal: CachedSignal) -> None:
        """Replace the entry for (signal.ticker, signal.timeframe). Atomic.

        We take a lock here just to keep two concurrent writers honest, but
        we don't expect writers to contend — the ComputeScheduler is the
        only writer."""
        key = (signal.ticker, signal.timeframe)
        with self._write_lock:
            self._entries[key] = signal

    def remove(self, ticker: TickerRef, timeframe: Timeframe) -> bool:
        """Drop an entry. Returns True if something was removed."""
        with self._write_lock:
            return self._entries.pop((ticker, timeframe), None) is not None

    # ---------- Test helpers ---------- #

    def clear(self) -> None:
        with self._write_lock:
            self._entries.clear()


def build_cached_signal(
    *,
    ticker: TickerRef,
    timeframe: Timeframe,
    latest_state: BarState,
    latest_close: float,
    state_history: list[BarState],
    last_change: ChangeSignal,
    last_change_bar: datetime | None,
    last_change_computed_at: datetime | None,
    computed_at: datetime,
    stale_since: datetime | None = None,
) -> CachedSignal:
    """Convenience factory that freezes the history into a tuple."""
    return CachedSignal(
        ticker=ticker,
        timeframe=timeframe,
        latest_state=latest_state,
        latest_close=latest_close,
        state_history=tuple(state_history),
        last_change=last_change,
        last_change_bar=last_change_bar,
        last_change_computed_at=last_change_computed_at,
        computed_at=computed_at,
        stale_since=stale_since,
    )
