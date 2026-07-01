"""RunQueue — in-memory FIFO queue of pending batch runs, with
per-exchange dedup and EoD-precedence semantics.

Design contract (operator spec):

  1. Order of insertion is preserved. If NSE enqueues before NYSE,
     NSE fires first.

  2. Per-exchange dedup: at any time, at most ONE entry per
     exchange exists in the queue. Enqueueing a second entry for
     the same exchange doesn't append — it merges with the existing
     entry.

  3. Kind precedence on merge (BOTH directions per Q3):
       existing=intraday, new=eod       -> merged=eod
       existing=eod,      new=intraday  -> merged=eod
       existing=intraday, new=intraday  -> unchanged
       existing=eod,      new=eod       -> unchanged
     EoD is the strict superset (all tiers vs intraday's tier-1),
     so EoD-wins in both directions.

  4. On merge, the queue POSITION of the existing entry is
     preserved. Only its kind may change. This means enqueueing
     "NSE:eod" when the queue is [NSE:intraday, NYSE:intraday]
     yields [NSE:eod, NYSE:intraday] — NSE stays at head.

  5. Thread-safe. All mutation goes through a single lock. A
     threading.Event is set on every enqueue so the dispatcher's
     wait_for_item loop can wake immediately instead of always
     sleeping to its 30s timeout.

  6. In-memory only (per Q1). Contents are lost on service
     restart. The scheduler thread (Phase 3) will re-enqueue at
     the next tick after restart, so the queue converges quickly.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal


Kind = Literal["intraday", "eod"]


@dataclass(frozen=True, slots=True)
class RunQueueItem:
    """One item in the queue."""
    exchange: str
    kind: Kind


# EoD is the strict superset per the operator spec.
# max_kind(a, b) returns whichever kind "wins" when both are
# enqueued for the same exchange.
def _max_kind(a: Kind, b: Kind) -> Kind:
    if a == "eod" or b == "eod":
        return "eod"
    return "intraday"


class RunQueue:
    """Thread-safe FIFO queue with per-exchange dedup.

    Public API:
      - enqueue(exchange, kind): merge-in-place or append. Returns
        one of 'inserted', 'kind_upgraded', 'noop_duplicate' to
        make caller-side logging + tests explicit.
      - pop_next() -> RunQueueItem | None: head-remove.
      - snapshot() -> list[RunQueueItem]: read-only copy for GUI /
        API responses.
      - wait_event: threading.Event set on every enqueue so the
        dispatcher can wake without polling.

    The wait_event is a *notification* signal, not a "queue
    non-empty" flag. It's Set on enqueue and left set — the
    dispatcher .clear()s it before checking the queue on each
    iteration, so subsequent enqueues re-set it. Standard
    condition-variable idiom, threading.Event-flavored.
    """

    def __init__(self) -> None:
        self._items: list[RunQueueItem] = []
        self._lock = threading.Lock()
        self.wait_event = threading.Event()

    def enqueue(self, exchange: str, kind: Kind) -> str:
        """Add or merge an item for `exchange`.

        Returns:
          'inserted'         — new item appended
          'kind_upgraded'    — existing item's kind changed
          'noop_duplicate'   — existing item already matches
        """
        with self._lock:
            for i, existing in enumerate(self._items):
                if existing.exchange == exchange:
                    merged_kind = _max_kind(existing.kind, kind)
                    if merged_kind == existing.kind:
                        # Nothing to change. Still set the event
                        # so the dispatcher wakes and re-checks
                        # (harmless — it'll find nothing new).
                        self.wait_event.set()
                        return "noop_duplicate"
                    # Preserve position; upgrade kind only.
                    self._items[i] = RunQueueItem(
                        exchange=exchange, kind=merged_kind,
                    )
                    self.wait_event.set()
                    return "kind_upgraded"

            # No existing entry — append.
            self._items.append(
                RunQueueItem(exchange=exchange, kind=kind),
            )
            self.wait_event.set()
            return "inserted"

    def pop_next(self) -> RunQueueItem | None:
        """Remove and return the head item, or None if empty."""
        with self._lock:
            if not self._items:
                return None
            return self._items.pop(0)

    def snapshot(self) -> list[RunQueueItem]:
        """Return an immutable-ish snapshot of the queue contents."""
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
