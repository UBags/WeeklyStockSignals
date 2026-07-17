"""Tests for the forward-walk orchestrator's pure seams.

The process-pool + DB path is exercised by the live-DB integration test; here we
pin the two pure pieces: worker-count resolution and the single-writer batch
loop (batching correctness is what keeps the parent from either OOM-buffering
every row or flushing one row at a time).
"""
from __future__ import annotations

from datetime import date

from trip_ema.domain.forward_walk import LONG, NEXT_SIGNAL, SignalForwardWalk
from trip_ema.fundamentals.forward_walk_orchestrator import (
    _resolve_processes,
    _write_in_batches,
)


def _rows(n: int) -> list[SignalForwardWalk]:
    return [
        SignalForwardWalk(i, LONG, date(2024, 1, 2), date(2024, 1, 3),
                          NEXT_SIGNAL, 1, False, None)
        for i in range(n)
    ]


def test_resolve_processes():
    assert _resolve_processes(cpu_logical=12) == 4     # NUC: 2/3 × 6 physical
    assert _resolve_processes(3) == 3                    # explicit request wins
    assert _resolve_processes(cpu_logical=4) == 1
    assert _resolve_processes(0, cpu_logical=12) == 4    # 0 → auto


def test_write_in_batches_flushes_by_size():
    calls: list[int] = []

    def up(rows: list) -> int:
        calls.append(len(rows))
        return len(rows)

    total = _write_in_batches([_rows(2) for _ in range(5)], up, batch_size=3)
    assert total == 10                       # all rows written exactly once
    assert sum(calls) == 10
    assert all(c >= 3 for c in calls[:-1])   # only the tail may be short


def test_write_in_batches_single_group_under_batch():
    calls: list[int] = []
    total = _write_in_batches(
        [_rows(2)], lambda r: (calls.append(len(r)) or len(r)), batch_size=100,
    )
    assert total == 2 and calls == [2]


def test_write_in_batches_empty():
    assert _write_in_batches([], lambda r: len(r), 100) == 0
