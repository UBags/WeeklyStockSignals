"""RunDispatcher — subprocess-launcher thread.

Reads pending items from RunQueue and spawns them one at a time,
gated on:

  1. `update_run_in_progress` table is empty (Q5 — one run
     globally at a time, until we get parallel-per-exchange).
  2. `backfill_runs.is_running()` is False (Q4).

For each dispatched item:

  1. INSERT into update_run_in_progress (exchange, kind).
  2. subprocess.Popen(['python', '-m', 'trip_ema.etl.cli',
                        'cycle', '--exchange', X, '--kind', Y]).
  3. Backfill the row's pid column with Popen.pid.
  4. Wait for the subprocess to exit (interruptibly, via a
     threading.Event that force-reset can set).
  5. DELETE the update_run_in_progress row.
  6. Loop.

Force-reset (`cancel_current`) is called from the admin HTTP route:
  - Sends SIGTERM to the current subprocess.
  - Waits up to 10 seconds.
  - Escalates to SIGKILL if still alive.
  - The main loop's subprocess.wait() returns; the finally deletes
    the row.

Thread lifecycle:
  - start()  — starts the daemon thread.
  - stop()   — sets the stop event. The dispatcher exits at its
    next queue-check (up to 30s). If a subprocess is running, it's
    left alone unless the caller ALSO calls cancel_current().
"""
from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from collections.abc import Callable

from sqlalchemy.engine import Engine

from ..persistence import transaction
from ..persistence.backfill_runs_repo import BackfillRunsRepository
from ..persistence.update_run_in_progress_repo import (
    UpdateRunInProgressRepository,
)
from .run_queue import RunQueue, RunQueueItem


logger = logging.getLogger(__name__)


DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_SIGTERM_GRACE_SECONDS = 10


@dataclass(slots=True)
class DispatcherState:
    """Snapshot of what the dispatcher is currently doing. Used
    for observability + the cancel_current path."""
    current_process: subprocess.Popen | None = None
    current_item: RunQueueItem | None = None
    current_row_id: int | None = None


class RunDispatcher:
    """Long-running thread that drains the RunQueue.

    Public API:
      - start() / stop()
      - is_alive() -> bool
      - cancel_current() -> bool
          Sends SIGTERM (then SIGKILL) to the currently running
          subprocess, if any. Returns True if something was killed.
          Idempotent — safe to call when nothing is running.
      - current_state() -> DispatcherState
          Read-only snapshot for observability.

    Injected dependencies (all optional, defaults are the
    production choices):
      - db_engine: SQLAlchemy Engine, required.
      - queue: RunQueue instance, required.
      - poll_interval_seconds: float, defaults to 30.
      - sigterm_grace_seconds: float, defaults to 10.
      - popen_factory: callable((args_list) -> subprocess.Popen).
          Overridden in tests so we don't actually spawn subprocesses.
      - python_executable: str path to the Python interpreter used
          when constructing the subprocess argv. Defaults to
          sys.executable (whatever's running the service).
    """

    def __init__(
        self, *,
        db_engine: Engine,
        queue: RunQueue,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        sigterm_grace_seconds: float = DEFAULT_SIGTERM_GRACE_SECONDS,
        popen_factory: Callable[
            [list[str]], subprocess.Popen
        ] | None = None,
        python_executable: str | None = None,
    ):
        self._db = db_engine
        self._queue = queue
        self._poll_interval = poll_interval_seconds
        self._sigterm_grace = sigterm_grace_seconds
        self._popen_factory = popen_factory or self._default_popen
        self._python = python_executable or sys.executable

        # Runtime state
        self._stop_event = threading.Event()
        self._state = DispatcherState()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---------- thread lifecycle ---------- #

    def start(self) -> None:
        """Start the dispatcher daemon thread. Idempotent — if
        already started, this is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            logger.info("RunDispatcher already running; start() ignored")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="RunDispatcher",
            daemon=True,
        )
        self._thread.start()
        logger.info("RunDispatcher started")

    def stop(self) -> None:
        """Signal the dispatcher to exit. Does NOT kill an in-flight
        subprocess — call cancel_current() for that."""
        self._stop_event.set()
        # Also nudge the queue's wait event so we exit the loop's
        # sleep immediately.
        self._queue.wait_event.set()
        logger.info("RunDispatcher stop requested")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------- inspection ---------- #

    def current_state(self) -> DispatcherState:
        """Read-only snapshot of what's currently running."""
        with self._state_lock:
            return DispatcherState(
                current_process=self._state.current_process,
                current_item=self._state.current_item,
                current_row_id=self._state.current_row_id,
            )

    # ---------- force-reset path ---------- #

    def cancel_current(self) -> bool:
        """Terminate the currently running subprocess, if any.

        SIGTERM → wait grace period → SIGKILL. The main loop's
        subprocess.wait() returns as soon as the process exits; its
        finally clause deletes the update_run_in_progress row.

        Idempotent. Safe to call when nothing is running (returns
        False). Thread-safe.

        Returns True if a subprocess was signalled, False otherwise.
        """
        with self._state_lock:
            proc = self._state.current_process
            item = self._state.current_item

        if proc is None or proc.poll() is not None:
            # Nothing to cancel, or already exited on its own.
            return False

        logger.warning(
            "cancel_current: sending SIGTERM to pid=%d "
            "(exchange=%s kind=%s)",
            proc.pid,
            item.exchange if item else "?",
            item.kind if item else "?",
        )
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            # Process already exited between our poll() and send.
            return False
        except Exception:
            logger.exception(
                "cancel_current: SIGTERM failed for pid=%d",
                proc.pid,
            )

        # Wait up to grace period for graceful exit.
        try:
            proc.wait(timeout=self._sigterm_grace)
            logger.info(
                "cancel_current: pid=%d exited within grace period",
                proc.pid,
            )
            return True
        except subprocess.TimeoutExpired:
            pass

        # Grace expired — SIGKILL.
        logger.warning(
            "cancel_current: SIGTERM grace expired, "
            "escalating to SIGKILL for pid=%d",
            proc.pid,
        )
        try:
            proc.kill()
        except ProcessLookupError:
            return False
        except Exception:
            logger.exception(
                "cancel_current: SIGKILL failed for pid=%d",
                proc.pid,
            )
            return False

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error(
                "cancel_current: pid=%d survived SIGKILL wait — "
                "leaving zombie; will reap on next Popen.wait()",
                proc.pid,
            )
        return True

    # ---------- main loop ---------- #

    def _run_loop(self) -> None:
        """The thread body. Wake on queue event or timeout, check
        gates, dispatch one item at a time."""
        while not self._stop_event.is_set():
            # Wait for something to happen. Clearing before wait
            # ensures we don't burn a wake if the event was set
            # while we were dispatching.
            self._queue.wait_event.clear()
            self._queue.wait_event.wait(timeout=self._poll_interval)

            if self._stop_event.is_set():
                break

            try:
                self._try_dispatch_one()
            except Exception:
                # Never let a per-iteration exception kill the thread.
                logger.exception(
                    "RunDispatcher iteration raised; continuing",
                )

        logger.info("RunDispatcher loop exiting")

    def _try_dispatch_one(self) -> None:
        """One iteration of the dispatch check + spawn."""
        # Gate 1: nothing else in progress.
        # Gate 2: no backfill running.
        with self._db.connect() as conn:
            in_progress_repo = UpdateRunInProgressRepository(conn)
            backfill_repo = BackfillRunsRepository(conn)
            if in_progress_repo.is_any_in_progress():
                return
            if backfill_repo.is_running():
                return

        item = self._queue.pop_next()
        if item is None:
            return

        # INSERT the in-progress marker. Do this in its own txn so
        # the pid backfill later can see the row.
        with transaction(self._db) as conn:
            row_id = UpdateRunInProgressRepository(conn).start(
                exchange=item.exchange, kind=item.kind,
            )

        # Spawn the cycle subprocess.
        argv = self._build_argv(item)
        try:
            proc = self._popen_factory(argv)
        except Exception:
            logger.exception(
                "RunDispatcher: failed to spawn cycle subprocess "
                "(exchange=%s kind=%s); cleaning up marker row",
                item.exchange, item.kind,
            )
            with transaction(self._db) as conn:
                UpdateRunInProgressRepository(conn).delete(
                    exchange=item.exchange,
                )
            return

        # Backfill pid.
        with transaction(self._db) as conn:
            UpdateRunInProgressRepository(conn).set_pid(
                row_id, proc.pid,
            )

        # Record the runtime state so cancel_current can act on it.
        with self._state_lock:
            self._state.current_process = proc
            self._state.current_item = item
            self._state.current_row_id = row_id

        logger.info(
            "RunDispatcher: spawned cycle pid=%d exchange=%s kind=%s",
            proc.pid, item.exchange, item.kind,
        )

        # Wait for exit. This blocks — but only this dispatcher
        # thread; the FastAPI event loop and the scheduler thread
        # are unaffected.
        try:
            rc = proc.wait()
            logger.info(
                "RunDispatcher: cycle exited pid=%d exchange=%s "
                "kind=%s rc=%d",
                proc.pid, item.exchange, item.kind, rc,
            )
        except Exception:
            logger.exception(
                "RunDispatcher: proc.wait() raised for pid=%d",
                proc.pid,
            )
        finally:
            # Always clean up the marker row, even on wait exception
            # or if cancel_current forced an early exit.
            try:
                with transaction(self._db) as conn:
                    UpdateRunInProgressRepository(conn).delete(
                        exchange=item.exchange,
                    )
            except Exception:
                logger.exception(
                    "RunDispatcher: failed to clear "
                    "update_run_in_progress row for exchange=%s "
                    "— may require manual force-reset",
                    item.exchange,
                )
            # Clear runtime state.
            with self._state_lock:
                self._state.current_process = None
                self._state.current_item = None
                self._state.current_row_id = None

    def _build_argv(self, item: RunQueueItem) -> list[str]:
        """Construct the `python -m trip_ema.etl.cli cycle ...`
        argv list for `item`.

        Extracted so tests can inspect it without spawning."""
        return [
            self._python, "-m", "trip_ema.etl.cli",
            "cycle",
            "--exchange", item.exchange,
            "--kind", item.kind,
        ]

    def _default_popen(self, argv: list[str]) -> subprocess.Popen:
        """Default Popen factory — real subprocess spawn.
        Tests override this."""
        return subprocess.Popen(
            argv,
            # Inherit stdout/stderr so log lines go to the same
            # log file/journalctl stream the service uses.
            stdout=None,
            stderr=None,
            # Start in a new process group so signals we send to
            # `proc.pid` don't accidentally propagate to the parent.
            start_new_session=True,
        )
