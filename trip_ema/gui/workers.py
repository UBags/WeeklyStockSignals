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
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

logger = logging.getLogger(__name__)


class _WorkerSignals(QObject):
    """Signals owned by the worker, separated because QRunnable
    can't itself emit signals (it's not a QObject)."""
    finished = Signal(object)
    failed = Signal(str)


class BackgroundWorker(QRunnable):
    """One-shot background task. Construct, call .signals.finished.connect(...),
    then submit to a QThreadPool via .pool.start(worker).

    Example:
        def fetch():
            return http_client.get_etl_status()
        worker = BackgroundWorker(fetch)
        worker.signals.finished.connect(on_status_received)
        worker.signals.failed.connect(on_status_error)
        QThreadPool.globalInstance().start(worker)
    """

    def __init__(self, fn: Callable[[], Any], *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as e:
            logger.exception("BackgroundWorker fn raised")
            self.signals.failed.emit(str(e))
            return
        self.signals.finished.emit(result)


def submit(fn: Callable[[], Any]) -> BackgroundWorker:
    """Convenience: build a worker, submit to the default thread pool,
    and return it so the caller can wire up the signals.

    Usage:
        w = submit(lambda: http_client.get_etl_status())
        w.signals.finished.connect(self._on_status)
        w.signals.failed.connect(self._on_status_error)
    """
    worker = BackgroundWorker(fn)
    QThreadPool.globalInstance().start(worker)
    return worker


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
        parent: Optional[QObject] = None,
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
