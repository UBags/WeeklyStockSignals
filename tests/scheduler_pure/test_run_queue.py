"""Unit tests for RunQueue — the in-memory FIFO with per-exchange
dedup and EoD-precedence semantics.

Pure logic tests — no DB, no threads, no subprocesses. Runs in the
DB-free slice.
"""
from __future__ import annotations

import threading

import pytest

from trip_ema.scheduler.run_queue import RunQueue, RunQueueItem, _max_kind


# ============================================================
# _max_kind helper
# ============================================================

class TestMaxKind:
    def test_intraday_intraday_yields_intraday(self):
        assert _max_kind("intraday", "intraday") == "intraday"

    def test_eod_eod_yields_eod(self):
        assert _max_kind("eod", "eod") == "eod"

    def test_intraday_then_eod_yields_eod(self):
        assert _max_kind("intraday", "eod") == "eod"

    def test_eod_then_intraday_yields_eod(self):
        # Q3 lock — EoD wins BOTH directions.
        assert _max_kind("eod", "intraday") == "eod"


# ============================================================
# enqueue / pop_next basics
# ============================================================

class TestEnqueueBasic:
    def test_enqueue_returns_inserted_for_new_exchange(self):
        q = RunQueue()
        assert q.enqueue("NSE", "intraday") == "inserted"

    def test_enqueue_returns_noop_for_exact_duplicate(self):
        q = RunQueue()
        q.enqueue("NSE", "intraday")
        assert q.enqueue("NSE", "intraday") == "noop_duplicate"

    def test_enqueue_returns_kind_upgraded_when_eod_replaces_intraday(self):
        q = RunQueue()
        q.enqueue("NSE", "intraday")
        assert q.enqueue("NSE", "eod") == "kind_upgraded"

    def test_pop_next_returns_none_when_empty(self):
        q = RunQueue()
        assert q.pop_next() is None

    def test_pop_next_returns_head_and_removes(self):
        q = RunQueue()
        q.enqueue("NSE", "intraday")
        item = q.pop_next()
        assert item == RunQueueItem("NSE", "intraday")
        assert q.pop_next() is None


# ============================================================
# FIFO ordering
# ============================================================

class TestFifoOrder:
    def test_multiple_exchanges_pop_in_insertion_order(self):
        q = RunQueue()
        q.enqueue("NSE", "intraday")
        q.enqueue("NYSE", "intraday")
        q.enqueue("BSE", "intraday")

        assert q.pop_next().exchange == "NSE"
        assert q.pop_next().exchange == "NYSE"
        assert q.pop_next().exchange == "BSE"
        assert q.pop_next() is None


# ============================================================
# Per-exchange dedup — position preservation
# ============================================================

class TestDedup:
    def test_second_intraday_for_same_exchange_is_noop(self):
        q = RunQueue()
        q.enqueue("NSE", "intraday")
        q.enqueue("NSE", "intraday")

        # Still one item.
        assert len(q.snapshot()) == 1
        assert q.snapshot()[0] == RunQueueItem("NSE", "intraday")

    def test_eod_after_intraday_upgrades_in_place(self):
        # Operator's canonical example:
        #   queue = [NSE:intraday, NYSE:intraday]
        #   enqueue(NSE, eod)
        #   -> [NSE:eod, NYSE:intraday]  (NSE stays at head)
        q = RunQueue()
        q.enqueue("NSE", "intraday")
        q.enqueue("NYSE", "intraday")
        q.enqueue("NSE", "eod")

        snap = q.snapshot()
        assert snap == [
            RunQueueItem("NSE", "eod"),
            RunQueueItem("NYSE", "intraday"),
        ]

    def test_intraday_after_eod_is_noop_kind_unchanged(self):
        # Q3 reverse direction — EoD stays.
        q = RunQueue()
        q.enqueue("NSE", "eod")
        result = q.enqueue("NSE", "intraday")

        assert result == "noop_duplicate"
        assert q.snapshot() == [RunQueueItem("NSE", "eod")]

    def test_position_preserved_across_multiple_upgrades(self):
        q = RunQueue()
        q.enqueue("NSE", "intraday")
        q.enqueue("NYSE", "intraday")
        q.enqueue("BSE", "intraday")

        # Upgrade the middle entry — must NOT move it.
        q.enqueue("NYSE", "eod")

        snap = q.snapshot()
        assert [i.exchange for i in snap] == ["NSE", "NYSE", "BSE"]
        assert snap[1].kind == "eod"


# ============================================================
# Snapshot immutability
# ============================================================

class TestSnapshot:
    def test_snapshot_is_a_copy_not_a_view(self):
        q = RunQueue()
        q.enqueue("NSE", "intraday")
        snap = q.snapshot()
        q.enqueue("NYSE", "intraday")

        # Original snapshot should be unchanged.
        assert len(snap) == 1
        assert snap[0].exchange == "NSE"

    def test_snapshot_reflects_current_state_when_taken(self):
        q = RunQueue()
        q.enqueue("NSE", "intraday")
        q.enqueue("NYSE", "intraday")
        snap = q.snapshot()

        assert [i.exchange for i in snap] == ["NSE", "NYSE"]


# ============================================================
# wait_event notification
# ============================================================

class TestWaitEvent:
    def test_enqueue_sets_wait_event(self):
        q = RunQueue()
        assert not q.wait_event.is_set()
        q.enqueue("NSE", "intraday")
        assert q.wait_event.is_set()

    def test_wait_event_set_even_on_noop_duplicate(self):
        # Design choice: a duplicate enqueue still nudges the
        # dispatcher. Harmless — it re-checks the queue and finds
        # nothing new.
        q = RunQueue()
        q.enqueue("NSE", "intraday")
        q.wait_event.clear()

        q.enqueue("NSE", "intraday")
        assert q.wait_event.is_set()


# ============================================================
# Thread safety — sanity check under concurrent enqueue
# ============================================================

class TestThreadSafety:
    def test_100_concurrent_enqueues_result_in_correct_dedup(self):
        # Spawn 10 threads, each enqueuing NSE:intraday 10 times.
        # Final state must be a single NSE:intraday item.
        q = RunQueue()

        def worker() -> None:
            for _ in range(10):
                q.enqueue("NSE", "intraday")

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(q.snapshot()) == 1
        assert q.snapshot()[0] == RunQueueItem("NSE", "intraday")

    def test_concurrent_enqueue_and_pop_no_lost_items(self):
        # Half the threads enqueue distinct exchanges; the other
        # half pop. Every enqueued exchange must be seen at most
        # once by the popping side — no duplicates, no ghosts.
        q = RunQueue()
        exchanges = [f"EX{i:02d}" for i in range(20)]
        popped: list[RunQueueItem] = []
        popped_lock = threading.Lock()

        def producer(ex: str) -> None:
            q.enqueue(ex, "intraday")

        def consumer() -> None:
            for _ in range(30):
                item = q.pop_next()
                if item is not None:
                    with popped_lock:
                        popped.append(item)

        producers = [threading.Thread(target=producer, args=(e,))
                     for e in exchanges]
        consumers = [threading.Thread(target=consumer) for _ in range(5)]

        for t in producers:
            t.start()
        for t in consumers:
            t.start()
        for t in producers:
            t.join()
        for t in consumers:
            t.join()

        # Drain any remaining items.
        while True:
            item = q.pop_next()
            if item is None:
                break
            popped.append(item)

        popped_exchanges = sorted(i.exchange for i in popped)
        assert popped_exchanges == sorted(exchanges), (
            "every enqueued exchange must be popped exactly once"
        )
