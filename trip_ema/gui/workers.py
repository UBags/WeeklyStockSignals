"""Background work primitives for the GUI.

PySide GUIs must not block the UI thread. We use two patterns:

  - BackgroundWorker: one-shot. Runs a callable on a QThreadPool;
    emits `finished(result)` on success or `failed(error_message)` on
    error. Used for action buttons, single fetches.

  - RefreshScheduler: QTimer wrapper. Runs a callback on a periodic
    cadence. The callback IS expected to spawn its own
    BackgroundWorker if it does I/O — the timer just fires the tick.
    Has manual-trigger and pause/resume.

Both are kept tiny — they're glue, not business logic.
"""

from __future__ import annotations

import logging
from typing import Any
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

logger = logging.getLogger(__name__)


class _WorkerSignals(QObject):
    """Signals owned by the worker, separated because QRunnable
    can't itself emit signals (it's not a QObject)."""
    finished = Signal(object)
    failed = Signal(str)


class BackgroundWorker(QRunnable):
    """One-shot background task. Prefer the `submit()` helper below
    over constructing this directly — it handles the lifetime and
    callback-ordering correctness properties that raw construction
    leaves to the caller (and that 22 call sites previously got wrong).

    If you do construct one directly: connect your callbacks BEFORE
    calling `pool.start(worker)`, and hold a strong reference to the
    worker until both signals have had a chance to deliver.
    """

    def __init__(self, fn: Callable[[], Any], *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = _WorkerSignals()
        # CRITICAL: QRunnable.autoDelete() defaults to True, which makes
        # QThreadPool delete the C++ side of the runnable as soon as
        # run() returns. Our `signals` QObject is owned by this wrapper,
        # so once Qt tears the C++ runnable down, queued-signal delivery
        # on the main thread can dispatch into freed memory → silent
        # process death with no Python traceback. The lifetime of this
        # worker is managed by `submit()` instead (see _INFLIGHT below).
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as e:
            logger.exception("BackgroundWorker fn raised")
            self.signals.failed.emit(str(e))
            return
        self.signals.finished.emit(result)


def submit(
    fn: Callable[[], Any],
    *,
    on_finished: Callable[[Any], None] | None = None,
    on_failed: Callable[[str], None] | None = None,
) -> BackgroundWorker:
    """Build a worker, wire its callbacks, submit to the default pool.

    Two correctness properties this function guarantees that callers
    used to have to get right themselves (and didn't):

      1. **Callbacks connect BEFORE start().** Otherwise, for any task
         that finishes faster than the caller's next line of Python
         (cached HTTP responses, small DB reads), the signal is emitted
         into nothing and the callback never fires. Symptom: panels
         that "sometimes don't refresh".

      2. **The worker is kept alive until both signals have had a
         chance to deliver.** Local `worker = submit(...)` variables go
         out of scope at the end of the calling method; combined with
         QRunnable's default autoDelete=True (now disabled in
         BackgroundWorker.__init__), this previously caused silent
         process death from use-after-free on the C++ QObject backing
         `worker.signals`. The module-level _INFLIGHT set holds a
         strong Python reference until the worker reports done.

    Usage:
        submit(
            lambda: http_client.get_etl_status(),
            on_finished=self._on_status,
            on_failed=self._on_status_error,
        )

    The old `w = submit(fn); w.signals.finished.connect(...)` pattern
    is still tolerated for backward compat, but it carries the
    fast-emit race described above — prefer the kwargs form.
    """
    worker = BackgroundWorker(fn)
    if on_finished is not None:
        worker.signals.finished.connect(on_finished)
    if on_failed is not None:
        worker.signals.failed.connect(on_failed)
    _INFLIGHT.add(worker)
    # Drop our strong ref once the worker has reported either way.
    # Using a default arg pins `worker` into the lambda's scope without
    # closing over the surrounding name (which is fine here, but kept
    # for clarity).
    worker.signals.finished.connect(lambda _r, w=worker: _INFLIGHT.discard(w))
    worker.signals.failed.connect(lambda _m, w=worker: _INFLIGHT.discard(w))
    QThreadPool.globalInstance().start(worker)
    return worker


# Strong references for in-flight workers. Without this, locals go out
# of scope at the end of the caller's method and the worker becomes
# eligible for GC the moment Python finishes the call frame — long
# before the background task finishes. See submit() for the full story.
_INFLIGHT: set[BackgroundWorker] = set()


class RefreshScheduler(QObject):
    """QTimer wrapper for periodic polling.

    Owns:
      - cadence_seconds: float
      - is_running: bool
      - tick callback (set via on_tick.connect)

    The callback runs on the GUI thread. If it's expensive, the
    callback should spawn a BackgroundWorker.
    """

    tick = Signal()

    def __init__(
        self, cadence_seconds: float, *,
        run_on_start: bool = True,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(int(cadence_seconds * 1000))
        self._timer.timeout.connect(self.tick.emit)
        self._run_on_start = run_on_start

    def start(self) -> None:
        if self._run_on_start:
            # Fire once immediately so the view populates without
            # waiting cadence_seconds.
            self.tick.emit()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def trigger_now(self) -> None:
        """Manual refresh button calls this."""
        self.tick.emit()

    @property
    def is_running(self) -> bool:
        return self._timer.isActive()

    @property
    def cadence_seconds(self) -> float:
        return self._timer.interval() / 1000.0

    def set_cadence(self, seconds: float) -> None:
        self._timer.setInterval(int(seconds * 1000))
