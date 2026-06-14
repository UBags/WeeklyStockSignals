"""Regression tests for the QRunnable autoDelete + callback-ordering fix
in trip_ema/gui/workers.py.

These tests lock in two correctness properties that were broken in v10
and earlier (see commit message for the patch that added them):

  1. Workers must keep the C++ side alive until both signals have had
     a chance to deliver. Before the fix, autoDelete=True (Qt default)
     caused silent process death via use-after-free.

  2. Callbacks passed via on_finished= / on_failed= must connect BEFORE
     pool.start(), so fast tasks (e.g. cached HTTP, in-memory work)
     can't emit-before-connect.

Both are tested under stress (many concurrent workers) since the bugs
were inherently racy and the original v10 code passed naive smoke tests.
"""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from trip_ema.gui.workers import BackgroundWorker, _INFLIGHT, submit


@pytest.fixture(autouse=True)
def _clean_inflight():
    """Don't let one test's lingering workers leak into the next."""
    _INFLIGHT.clear()
    yield
    # Wait for any spawned workers to finish before clearing.
    QThreadPool.globalInstance().waitForDone(2000)
    _INFLIGHT.clear()


def _drain(app: QApplication, until, timeout_ms: int = 5000):
    """Spin the event loop until `until()` is true or the timeout fires."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while not until() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)


def test_autoDelete_is_disabled(qapp):
    """The fix hinges on this: if a future refactor re-enables
    autoDelete the silent-crash bug returns."""
    w = BackgroundWorker(lambda: 1)
    assert w.autoDelete() is False, (
        "BackgroundWorker.autoDelete must stay False, otherwise QThreadPool "
        "deletes the C++ side of the runnable as soon as run() returns and "
        "queued-signal delivery dispatches into freed memory."
    )


def test_callback_fires_for_fast_task(qapp):
    """Smoke test: the kwargs form connects callbacks before start(),
    so even an instant task delivers its result."""
    received = []
    submit(lambda: 42, on_finished=received.append)
    _drain(qapp, lambda: len(received) == 1)
    assert received == [42]


def test_failed_callback_fires(qapp):
    """The failure path goes through the same connect-before-start guard."""
    errors = []
    def boom():
        raise RuntimeError("intentional")
    submit(boom, on_failed=errors.append)
    _drain(qapp, lambda: len(errors) == 1)
    assert len(errors) == 1
    assert "intentional" in errors[0]


def test_no_callbacks_dropped_under_stress(qapp):
    """The original repro: hammer the pool with many fast tasks. Under
    v10's code 1998 of 2000 callbacks were silently dropped (proven
    via a headless repro). This test requires 100% delivery.

    Note: this exercises the connect-before-start guarantee. The
    autoDelete=False guarantee is locked in by
    test_autoDelete_is_disabled; both are load-bearing for the fix."""
    N = 500
    received: list[int] = []

    for i in range(N):
        submit(lambda i=i: i, on_finished=received.append)

    _drain(qapp, lambda: len(received) == N, timeout_ms=15000)
    assert len(received) == N, (
        f"Only {len(received)}/{N} callbacks delivered. The "
        f"connect-before-start or INFLIGHT retention guarantee has regressed."
    )


def test_inflight_drains_after_completion(qapp):
    """The _INFLIGHT registry must release workers after their signals
    fire, otherwise we'd leak references for the life of the process."""
    received = []
    for _ in range(50):
        submit(lambda: "ok", on_finished=received.append)
    _drain(qapp, lambda: len(received) == 50, timeout_ms=5000)
    # Give the cleanup lambdas a chance to run (they're connected too).
    _drain(qapp, lambda: len(_INFLIGHT) == 0, timeout_ms=2000)
    assert len(_INFLIGHT) == 0, (
        f"_INFLIGHT still holds {len(_INFLIGHT)} workers after all callbacks "
        f"delivered — strong-ref retention is leaking."
    )


def test_legacy_pattern_still_works(qapp):
    """Backward-compat: the old `w = submit(fn); w.signals.X.connect(...)`
    pattern is still tolerated. It carries a fast-emit race but should
    work for slow-enough tasks. Use a small sleep to dodge the race."""
    received = []

    def slow():
        time.sleep(0.05)
        return "done"

    worker = submit(slow)
    worker.signals.finished.connect(received.append)
    _drain(qapp, lambda: len(received) == 1, timeout_ms=3000)
    assert received == ["done"]
