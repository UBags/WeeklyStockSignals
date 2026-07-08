"""Tests for the shared fan_out primitive.

Pure-compute workers (no DB) — this exercises the fan-out mechanics:
error isolation, the done/failed contract, progress heartbeats, and
worker-count resolution. DB-backed parallelism is covered by the
stage-level tests that use fan_out.
"""
from __future__ import annotations

import threading

import pytest

from trip_ema.fundamentals.parallel import (
    FanOutOutcome,
    fan_out,
    resolve_max_workers,
)


def test_all_succeed():
    seen: list[int] = []
    lock = threading.Lock()

    def work(x: int) -> None:
        with lock:
            seen.append(x)

    out = fan_out(list(range(50)), work, label="t", max_workers=4)
    assert isinstance(out, FanOutOutcome)
    assert out.done == 50
    assert out.failed == []
    assert sorted(seen) == list(range(50))


def test_one_failure_isolated():
    def work(x: int) -> None:
        if x == 7:
            raise ValueError("boom")

    out = fan_out(list(range(20)), work, label="t", max_workers=4)
    assert out.done == 19
    assert len(out.failed) == 1
    item, err = out.failed[0]
    assert item == 7
    assert "ValueError: boom" in err


def test_multiple_failures_all_collected():
    bad = {2, 5, 11}

    def work(x: int) -> None:
        if x in bad:
            raise RuntimeError(f"fail-{x}")

    out = fan_out(list(range(15)), work, label="t", max_workers=8)
    assert out.done == 15 - len(bad)
    assert {item for item, _ in out.failed} == bad


def test_empty_list_is_noop():
    calls: list[tuple[str, str]] = []
    out = fan_out(
        [], lambda x: None, label="t",
        progress_cb=lambda lbl, msg: calls.append((lbl, msg)),
    )
    assert out.done == 0
    assert out.failed == []
    # Emits a single "nothing to do" heartbeat.
    assert calls and "0/0" in calls[0][1]


def test_progress_cb_fires_and_final_is_total():
    msgs: list[str] = []

    def work(x: int) -> None:
        pass

    fan_out(
        list(range(200)), work, label="stage_x", max_workers=4,
        progress_cb=lambda lbl, msg: msgs.append(msg),
        heartbeat_pct=10,
    )
    assert msgs, "expected at least one heartbeat"
    # The final heartbeat always reports total/total.
    assert "200/200" in msgs[-1]


def test_progress_cb_exception_does_not_abort():
    def bad_cb(lbl: str, msg: str) -> None:
        raise RuntimeError("cb broke")

    out = fan_out(
        list(range(10)), lambda x: None, label="t",
        max_workers=2, progress_cb=bad_cb,
    )
    assert out.done == 10


def test_as_tuple_matches_stage_contract():
    out = fan_out(list(range(3)), lambda x: None, label="t")
    done, failed = out.as_tuple()
    assert done == 3
    assert failed == []


@pytest.mark.parametrize(
    "requested,expected_max",
    [(1, 1), (3, 3), (999, 12), (0, None), (None, None)],
)
def test_resolve_max_workers_respects_request_and_cap(
    requested, expected_max,
):
    n = resolve_max_workers(requested)
    assert n >= 1
    assert n <= 12
    if expected_max is not None:
        assert n == expected_max


def test_resolve_default_is_reasonable():
    n = resolve_max_workers(None)
    assert 2 <= n <= 12


def test_workers_never_exceed_item_count():
    # 2 items but many workers requested — must not spin up 8 threads.
    names: set[str] = set()
    lock = threading.Lock()

    def work(x: int) -> None:
        with lock:
            names.add(threading.current_thread().name)

    fan_out([1, 2], work, label="t", max_workers=8)
    assert len(names) <= 2
