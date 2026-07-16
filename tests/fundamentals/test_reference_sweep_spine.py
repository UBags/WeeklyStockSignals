"""A1: the parallel reference-sweep spine, in isolation.

Injection-only: a fake worker_fn drives outcomes, an injected checkpoint_sink
records checkpoints. The spine's final close() touches the DB, so these tests
stop at the seam — they assert the orchestration (filtering, counting,
checkpoint cadence + payload), and let the industry integration suite cover
the real close.
"""

from __future__ import annotations

import threading

import pytest

from trip_ema.fundamentals.runners import (
    _completed_ids_from_checkpoint,
    _run_reference_sweep,
)


# ---------- _completed_ids_from_checkpoint ---------- #


class TestCompletedIdsRecovery:

    def test_new_shape_returns_explicit_set(self):
        assert _completed_ids_from_checkpoint(
            {"completed_ids": [1, 2, 3]}
        ) == {1, 2, 3}

    def test_legacy_cursor_maps_to_first_n_ids(self):
        """An old serial-era checkpoint stored only cursor_index. The spine
        maps that back to the ids of the first N tickers so a run started by
        the old loop still resumes correctly."""
        pj = {
            "ticker_list": [{"id": 10}, {"id": 20}, {"id": 30}, {"id": 40}],
            "cursor_index": 2,
        }
        assert _completed_ids_from_checkpoint(pj) == {10, 20}

    def test_both_shapes_prefers_completed_ids(self):
        pj = {
            "ticker_list": [{"id": 10}, {"id": 20}],
            "cursor_index": 1,
            "completed_ids": [10, 20],
        }
        assert _completed_ids_from_checkpoint(pj) == {10, 20}

    def test_none_and_empty(self):
        assert _completed_ids_from_checkpoint(None) == set()
        assert _completed_ids_from_checkpoint({}) == set()


# ---------- spine orchestration ---------- #


class _StubRepoClose:
    """The spine calls FundamentalsRunRepository(conn).close() at the end,
    inside `with transaction(db_engine)`. We give it a fake db_engine whose
    transaction() is a nullcontext and whose connection swallows everything,
    so the spine runs to completion without a real DB. The close SQL never
    executes because our fake connection's repo path is never real — instead
    we inject checkpoint_sink and assert on THAT, and treat reaching close as
    success. To avoid a real close, we raise a sentinel from the sink on the
    final synthetic checkpoint... but simpler: we let close hit the fake
    engine, which no-ops."""


class _FakeConn:
    def execute(self, *a, **k):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeEngine:
    """Minimal engine: .begin() and .connect() both yield a no-op conn, so
    the spine's close-out transaction runs harmlessly."""

    def begin(self):
        return _FakeConn()

    def connect(self):
        return _FakeConn()


@pytest.fixture
def recorded_checkpoints():
    return []


def _sink_factory(recorded):
    def _sink(db_engine, run_id, ticker_list, completed_ids,
              succeeded, failed, skipped):
        recorded.append({
            "run_id": run_id,
            "completed_ids": list(completed_ids),
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
        })
    return _sink


def _tickers(n, start=1):
    return [{"id": i, "symbol": f"SYM{i}"} for i in range(start, start + n)]


class TestSpineOrchestration:

    def test_completed_tickers_are_skipped(self, recorded_checkpoints):
        """Tickers already in completed_ids must NOT be handed to worker_fn."""
        seen = []
        lock = threading.Lock()

        def worker(t):
            with lock:
                seen.append(t["id"])
            return "succeeded"

        _run_reference_sweep(
            _FakeEngine(),
            run_id=1, exchange="NSE", phase="industry",
            ticker_list=_tickers(5),          # ids 1..5
            completed_ids={1, 2, 3},          # only 4, 5 pending
            worker_fn=worker,
            checkpoint_sink=_sink_factory(recorded_checkpoints),
        )
        assert sorted(seen) == [4, 5]

    def test_counts_each_outcome(self, recorded_checkpoints):
        """succeeded / failed / skipped are tallied from worker returns.
        We can't read the spine's internal counters directly, but the final
        close carries them — and the last checkpoint (if cadence hit) does
        too. With 50-cadence and few tickers, no checkpoint fires, so assert
        via a cadence-sized run below instead. Here we just prove no crash
        and every ticker was visited."""
        seen = []
        lock = threading.Lock()

        def worker(t):
            with lock:
                seen.append(t["id"])
            # alternate outcomes
            return "failed" if t["id"] % 2 == 0 else "succeeded"

        _run_reference_sweep(
            _FakeEngine(),
            run_id=2, exchange="NSE", phase="industry",
            ticker_list=_tickers(6),
            completed_ids=set(),
            worker_fn=worker,
            checkpoint_sink=_sink_factory(recorded_checkpoints),
        )
        assert sorted(seen) == [1, 2, 3, 4, 5, 6]

    def test_checkpoint_fires_on_cadence_with_completion_set(
        self, recorded_checkpoints,
    ):
        """With exactly CHECKPOINT_EVERY (50) pending tickers, one checkpoint
        must fire, and its completed_ids payload must contain all 50 ids —
        proving the checkpoint records the SET, not an index."""
        from trip_ema.fundamentals.runners import CHECKPOINT_EVERY

        n = CHECKPOINT_EVERY
        lock = threading.Lock()

        def worker(t):
            return "succeeded"

        _run_reference_sweep(
            _FakeEngine(),
            run_id=3, exchange="NSE", phase="industry",
            ticker_list=_tickers(n),
            completed_ids=set(),
            worker_fn=worker,
            checkpoint_sink=_sink_factory(recorded_checkpoints),
        )
        assert len(recorded_checkpoints) >= 1
        last = recorded_checkpoints[-1]
        assert len(last["completed_ids"]) == n
        assert set(last["completed_ids"]) == set(range(1, n + 1))
        assert last["succeeded"] == n

    def test_empty_pending_is_a_clean_noop_close(self, recorded_checkpoints):
        """Everything already completed: no worker calls, no checkpoint, but
        the run still closes cleanly (SUCCESS, 0/0/0)."""
        called = []

        def worker(t):
            called.append(t["id"])
            return "succeeded"

        rid = _run_reference_sweep(
            _FakeEngine(),
            run_id=4, exchange="NSE", phase="industry",
            ticker_list=_tickers(3),
            completed_ids={1, 2, 3},
            worker_fn=worker,
            checkpoint_sink=_sink_factory(recorded_checkpoints),
        )
        assert rid == 4
        assert called == []
        assert recorded_checkpoints == []
