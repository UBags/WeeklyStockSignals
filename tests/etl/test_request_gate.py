"""RequestGate: process-wide spacing of outbound provider requests."""
from __future__ import annotations

import threading
import time
from datetime import date

import pytest

from trip_ema.etl.request_gate import RequestGate
from trip_ema.etl.yfinance_source import YFinanceSource


class _FakeClock:
    """Monotonic clock that only advances when a sleep is charged to it."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.slept.append(d)
        self.t += d


def test_zero_gap_is_a_noop() -> None:
    clock = _FakeClock()
    gate = RequestGate(0.0, clock=clock.now, sleep=clock.sleep)
    assert gate.wait() == 0.0
    assert gate.wait() == 0.0
    assert clock.slept == []


def test_negative_gap_is_clamped_to_zero() -> None:
    assert RequestGate(-5.0).gap_seconds == 0.0


def test_serial_calls_are_spaced_by_the_gap() -> None:
    clock = _FakeClock()
    gate = RequestGate(5.0, clock=clock.now, sleep=clock.sleep)
    assert gate.wait() == 0.0            # first call reserves, waits nothing
    assert gate.wait() == pytest.approx(5.0)
    assert gate.wait() == pytest.approx(5.0)
    assert clock.slept == pytest.approx([5.0, 5.0])


def test_slow_caller_does_not_accumulate_debt() -> None:
    """A caller returning after the gap already elapsed waits zero -- the gate
    spaces starts, it does not tax every call."""
    clock = _FakeClock()
    gate = RequestGate(5.0, clock=clock.now, sleep=clock.sleep)
    gate.wait()
    clock.t += 60.0
    assert gate.wait() == 0.0


def test_concurrent_threads_never_start_closer_than_the_gap() -> None:
    """The requirement: two threads are never inside `gap` of each other."""
    gap = 0.05
    gate = RequestGate(gap)
    starts: list[float] = []
    lock = threading.Lock()
    ready = threading.Barrier(8)

    def worker() -> None:
        ready.wait()
        gate.wait()
        with lock:
            starts.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(starts) == 8
    starts.sort()
    # Index walk, not zip: pairing a list with its own tail gives N and
    # N-1, so strict=True raises before the assertion is ever reached.
    deltas = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    assert min(deltas) >= gap * 0.9, deltas


class _RecordingGate:
    """Collaborator stub: records that wait() ran, in call order."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls = 0

    def wait(self) -> float:
        self.calls += 1
        self.order.append("gate")
        return 0.0


def _source(order: list[str], gate: _RecordingGate) -> YFinanceSource:
    def _download(**_kwargs):
        order.append("download")
        raise RuntimeError("stop after the gate -- ordering is the assertion")

    return YFinanceSource(
        exchange="NSE",
        batch_size=10,
        max_retries=1,
        download=_download,
        sleep=lambda _s: None,
        request_gate=gate,
    )


def test_daily_fetch_waits_on_the_gate_before_every_download() -> None:
    order: list[str] = []
    gate = _RecordingGate(order)
    _source(order, gate).fetch_daily_history(
        ["ADANIPORTS"], start=date(2024, 1, 1), end=date(2024, 2, 1),
    )
    assert gate.calls == 1
    assert order == ["gate", "download"]


def test_hourly_fetch_waits_on_the_gate_before_every_download() -> None:
    order: list[str] = []
    gate = _RecordingGate(order)
    _source(order, gate).fetch_hourly_recent(["ADANIPORTS"], lookback_days=5)
    assert gate.calls == 1
    assert order == ["gate", "download"]


def test_source_without_a_gate_is_ungated() -> None:
    """Default construction must not change existing behaviour."""
    order: list[str] = []

    def _download(**_kwargs):
        order.append("download")
        raise RuntimeError("stop")

    YFinanceSource(
        exchange="NSE", batch_size=10, max_retries=1,
        download=_download, sleep=lambda _s: None,
    ).fetch_daily_history(
        ["ADANIPORTS"], start=date(2024, 1, 1), end=date(2024, 2, 1),
    )
    assert order == ["download"]
