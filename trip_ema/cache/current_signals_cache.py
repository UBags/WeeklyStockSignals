"""V1 CurrentSignalsCache — in-memory mirror of `current_signals`.

A thin, V1-shaped cache that stores one V1 `SignalEvent` per
`(ticker_id, timeframe)`. The cycle (Step 6's `recompute_market`)
calls `rebuild_from` at the end of every tick, atomically replacing
the entire dict. Readers (the V1 SignalsReader from Step 1, and the
Step 12 GUI rewire) get cache-hit O(1) lookups without touching
the DB.

Coexistence note:
  - The legacy `trip_ema.cache.signal_cache.SignalCache` continues to
    live alongside this for now. It stores rich CachedSnapshot objects
    (indicator_state + signal_event + stale flags) and is still wired
    into the vestigial `trip_ema.scheduler.compute_scheduler` which
    never calls `.start()` in V1. Step 10 of the V1 cutover deletes
    ComputeScheduler and the legacy cache together.

Lock semantics:
  - Writes (`rebuild_from`): single writer expected (the cycle).
    Implementation does a local-build-then-single-reassign so the
    swap is GIL-atomic. Defensive `_write_lock` exists for the
    unlikely case of a second writer; cost is negligible.
  - Reads (`get_signal`, `all_signals`): lock-free, rely on
    dict-lookup atomicity under the GIL. A read in flight during
    a rebuild sees either the OLD dict OR the NEW dict, never a
    torn intermediate.
"""
from __future__ import annotations

import threading
from typing import Sequence

from ..domain.signal_event import SignalEvent


class CurrentSignalsCache:
    """In-memory mirror of `current_signals`. Keyed by
    `(ticker_id, timeframe)`."""

    def __init__(self) -> None:
        # The internal dict. Replaced wholesale by `rebuild_from`.
        # Readers MUST go through `get_signal` / `all_signals` so the
        # atomic-swap invariant holds.
        self._snapshots: dict[tuple[int, str], SignalEvent] = {}
        # Defensive write lock — single writer expected, but cheap
        # to guard against bugs that might introduce a second writer
        # later.
        self._write_lock = threading.Lock()

    # ---------- reads (lock-free) ---------- #

    def get_signal(
        self, ticker_id: int, timeframe: str,
    ) -> SignalEvent | None:
        """O(1) lookup. None if not in cache (i.e., the ticker did
        not change regime in the latest cycle, OR the cache hasn't
        been built yet)."""
        return self._snapshots.get((ticker_id, timeframe))

    def all_signals(self) -> list[SignalEvent]:
        """Atomic copy of every cached SignalEvent.

        Returns a new list each call; the caller can mutate it without
        affecting the cache. Used by the email composer (Step 7) and
        any "list all current signals" admin views.
        """
        return list(self._snapshots.values())

    def signal_count(self) -> int:
        """Number of cached entries. Equivalent to
        `len(current_signals)` for the latest cycle."""
        return len(self._snapshots)

    # ---------- writes ---------- #

    def rebuild_from(self, signals: Sequence[SignalEvent]) -> None:
        """Atomically replace the cache from a fresh sequence of
        signals.

        Builds the new dict locally, then swaps it in via a single
        assignment. The swap is GIL-atomic — concurrent readers see
        either the OLD dict OR the NEW dict, never a partial mix.

        Tickers that were in the old cache but are NOT in `signals`
        are dropped (no stale_since flag — they're just gone). This
        matches the V1 model: the cache holds only THIS cycle's
        signals; a ticker that hasn't flipped this cycle is simply
        absent.

        Args:
            signals: the V1 SignalEvent rows from `current_signals`.
                Empty input is valid — cache is emptied.
        """
        new_snapshots: dict[tuple[int, str], SignalEvent] = {
            (s.ticker_id, s.timeframe): s for s in signals
        }
        with self._write_lock:
            self._snapshots = new_snapshots
