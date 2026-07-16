"""Shared fixtures for GUI tests.

We force the 'offscreen' Qt platform so the tests can run in CI /
containers without a display. The QT_QPA_PLATFORM env var is set
at module import time, before PySide6 is loaded by pytest-qt.

Teardown discipline (Phase 12):
  PySide6 wraps Qt's C++ object hierarchy. Every QWidget exists as
  both a Python wrapper (managed by Python's GC) and a C++ object
  (managed by Qt's parent-child hierarchy). At test teardown, two
  things race:
    1. pytest-qt destroys the QApplication
    2. Python's GC tries to clean up dangling QWidget wrappers
  If (2) runs after (1), the wrapper tries to call C++ destructors
  on memory the QApplication has already torn down → SIGSEGV.

  qtbot.addWidget(w) helps — it calls w.close() at teardown — but
  it does NOT call w.deleteLater() and does NOT pump the event
  loop afterwards. deleteLater() only schedules deletion; without
  processEvents(), the deletion sits in the queue until the
  QApplication tears down at session end, which is exactly when
  the race fires.

  This conftest defines two fixtures to mitigate the race:

    - managed_widget(factory): an opt-in helper for tests that
      want explicit control over a single top-level widget. Calls
      on_close() (if defined), close(), deleteLater(), then
      processEvents(). Returns the live widget; teardown runs in
      reverse on exit.

    - _drain_after_every_gui_test (autouse): runs after every
      test. Pumps the event loop with deferred-deletion handling
      so any QWidget the test registered via qtbot.addWidget (or
      otherwise instantiated) gets its deleteLater() events
      drained before the next test starts.

  Empirical impact: the conftest improves standalone tests/gui
  reliability from ~3/5 to ~4-5/5 clean exits. It does NOT fully
  eliminate the race because some final QApplication teardown
  happens inside pytest's atexit handlers, after any per-test
  fixture has run. The CI workflow's two-invocation pattern (run
  tests/gui in its own pytest session) plus the output-parsing
  wrapper remain necessary as belt-and-braces.

  See PHASE11_CHANGELOG.md "pytest-qt teardown segfault" for
  historical context.
"""

from __future__ import annotations

import gc
import os
from typing import TypeVar
from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt, QEventLoop
from PySide6.QtWidgets import QApplication, QWidget


W = TypeVar("W", bound=QWidget)


def _drain_qt_event_loop() -> None:
    """Force Qt to process every pending event including DeferredDelete.

    Calling deleteLater() on a QObject schedules a DeferredDelete
    event; the C++ object isn't actually freed until that event is
    processed. We must drain it before the QApplication itself goes
    away. Two passes is the standard Qt pattern: the first runs
    deferred-deletes scheduled by the test, the second runs anything
    scheduled by the first pass's destructors.
    """
    app = QApplication.instance()
    if app is None:
        return
    flags = QEventLoop.ProcessEventsFlag.AllEvents
    app.processEvents(flags)
    app.sendPostedEvents(None, 0)  # 0 = all event types
    app.processEvents(flags)


@pytest.fixture(scope="session", autouse=True)
def _isolate_qsettings_per_worker(tmp_path_factory):
    """Give each xdist worker its own QSettings store.

    QSettings is a per-user global — one INI (or registry key) per
    organization+application, shared by every worker PROCESS under
    `pytest -n`. The per-test _clean_qsettings fixtures only isolate WITHIN a
    process; a concurrent worker writing the same key between another test's
    clear and its assertion leaks across workers (e.g. the exchange picker
    restoring a stale exchange). Redirecting each worker to a private temp INI
    removes the shared store, so GUI tests are order-independent under xdist.
    """
    from PySide6.QtCore import QSettings

    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    base = tmp_path_factory.getbasetemp() / f"qsettings-{worker}"
    base.mkdir(parents=True, exist_ok=True)
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(
        QSettings.IniFormat, QSettings.UserScope, str(base),
    )
    yield


@pytest.fixture
def managed_widget():
    """Factory fixture for opt-in safe widget management.

    Usage:
        def test_thing(qtbot, managed_widget):
            view = managed_widget(lambda: EtlStatusView(...))
            qtbot.addWidget(view)
            # ... assertions ...
            # No cleanup needed — fixture handles it.

    The factory takes a zero-arg callable that constructs the widget;
    we do it this way rather than receiving the widget directly so
    construction errors are attributed to the test, not the fixture.
    """
    created: list[QWidget] = []

    def _factory(construct: Callable[[], W]) -> W:
        widget = construct()
        # WA_DeleteOnClose makes close() also schedule C++ deletion.
        # Without this, close() only hides the widget; the C++
        # object outlives the Python wrapper.
        widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        created.append(widget)
        return widget

    yield _factory

    # Teardown — most-recently-created first (LIFO), which roughly
    # matches the natural construction-order dependency.
    for widget in reversed(created):
        # 1. Stop background workers (views implement on_close()).
        if hasattr(widget, "on_close"):
            try:
                widget.on_close()
            except Exception:
                pass  # Tests should not fail in teardown
        # 2. Close (also schedules C++ deletion via WA_DeleteOnClose)
        try:
            widget.close()
        except RuntimeError:
            pass  # C++ side already gone
        # 3. Explicit deleteLater for belt-and-braces.
        try:
            widget.deleteLater()
        except RuntimeError:
            pass

    # 4. Drain Qt's event loop so DeferredDelete events fire.
    _drain_qt_event_loop()


@pytest.fixture(autouse=True)
def _drain_after_every_gui_test():
    """Belt-and-braces: even tests that don't use managed_widget
    get their dangling widgets cleaned up. Runs unconditionally
    after every test in tests/gui/.

    qtbot.addWidget(w) registers w for close() at teardown but
    doesn't deleteLater() or processEvents(). This fixture fills
    that gap regardless of how the test wired things up.
    """
    yield
    _drain_qt_event_loop()
    # One last GC pass nudges Python wrappers whose C++ siblings
    # were just deleted. Without this, a test that holds a local
    # reference to a deleted QObject can re-trigger the race when
    # the local goes out of scope on the next test's stack frame.
    gc.collect()
    _drain_qt_event_loop()
