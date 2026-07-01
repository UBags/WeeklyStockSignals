"""Integration-style tests for RunDispatcher.

We don't spawn real subprocesses — the popen_factory is stubbed to
return a fake Popen-shaped object we control. What we DO use for
real: SQLAlchemy + a real DB engine via the db_engine fixture, so
the marker-row lifecycle is tested against actual PostgreSQL.

Tests cover:
  - Gating: skips dispatch when update_run_in_progress non-empty
  - Gating: skips dispatch when backfill_runs.is_running() True
  - Happy path: pops queue item, INSERTs marker, spawns, sets pid,
    waits, DELETEs marker
  - cancel_current: sends SIGTERM, escalates to SIGKILL after grace
  - cancel_current on nothing: returns False, no crash
  - Spawn failure: cleans up marker row
"""
from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from trip_ema.persistence import transaction
from trip_ema.persistence.update_run_in_progress_repo import (
    UpdateRunInProgressRepository,
)
from trip_ema.scheduler.run_dispatcher import RunDispatcher
from trip_ema.scheduler.run_queue import RunQueue


# ============================================================
# Fake Popen — records signals, exposes controllable exit
# ============================================================

class FakePopen:
    """Popen shim with just enough surface for RunDispatcher."""

    def __init__(self, pid: int = 12345):
        self.pid = pid
        self._returncode: int | None = None
        self._exit_event = threading.Event()
        self.signals_received: list[int] = []
        self.killed = False

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._exit_event.wait(timeout=timeout):
            return self._returncode or 0
        raise subprocess.TimeoutExpired(cmd=[], timeout=timeout or 0)

    def send_signal(self, sig: int) -> None:
        self.signals_received.append(sig)

    def kill(self) -> None:
        self.killed = True
        self._simulate_exit(-9)

    # Test helpers

    def _simulate_exit(self, rc: int = 0) -> None:
        self._returncode = rc
        self._exit_event.set()


# ============================================================
# Helpers
# ============================================================

def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02):
    """Spin until predicate() is truthy or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    pytest.fail(f"predicate not satisfied within {timeout}s")


def _clear_tables(engine) -> None:
    with transaction(engine) as conn:
        conn.execute(text("DELETE FROM update_run_in_progress"))
        conn.execute(text("DELETE FROM backfill_runs"))


# ============================================================
# Gating
# ============================================================

class TestGating:
    def test_skips_when_update_run_in_progress_non_empty(
        self, db_engine,
    ):
        _clear_tables(db_engine)
        # Pre-seed a row so the gate blocks.
        with transaction(db_engine) as conn:
            UpdateRunInProgressRepository(conn).start(
                exchange="NSE", kind="intraday",
            )

        queue = RunQueue()
        queue.enqueue("NYSE", "intraday")

        popen_calls: list[list[str]] = []
        def popen_factory(argv):
            popen_calls.append(argv)
            return FakePopen()

        dispatcher = RunDispatcher(
            db_engine=db_engine, queue=queue,
            poll_interval_seconds=0.1,
            popen_factory=popen_factory,
        )
        dispatcher.start()
        time.sleep(0.5)   # let a couple of poll cycles run
        dispatcher.stop()
        dispatcher._thread.join(timeout=2)

        assert popen_calls == [], (
            "dispatcher must not spawn while update_run_in_progress "
            "has any row"
        )
        # Queue item still present.
        assert len(queue) == 1
        _clear_tables(db_engine)

    def test_skips_when_backfill_running(self, db_engine):
        _clear_tables(db_engine)
        # Seed a backfill_runs row with finished_at IS NULL.
        with transaction(db_engine) as conn:
            conn.execute(
                text("""
                    INSERT INTO backfill_runs
                        (pid, exchange, date_from, date_to,
                         refetch_bars, ticker_scope, started_at)
                    VALUES
                        (:pid, :ex, :df, :dt, :rf, :ts, NOW())
                """),
                {
                    "pid": 999, "ex": "NSE",
                    "df": "2026-01-01", "dt": "2026-07-01",
                    "rf": False, "ts": "ALL",
                },
            )

        queue = RunQueue()
        queue.enqueue("NSE", "intraday")

        popen_calls: list[list[str]] = []
        def popen_factory(argv):
            popen_calls.append(argv)
            return FakePopen()

        dispatcher = RunDispatcher(
            db_engine=db_engine, queue=queue,
            poll_interval_seconds=0.1,
            popen_factory=popen_factory,
        )
        dispatcher.start()
        time.sleep(0.5)
        dispatcher.stop()
        dispatcher._thread.join(timeout=2)

        assert popen_calls == []
        assert len(queue) == 1
        _clear_tables(db_engine)


# ============================================================
# Happy path
# ============================================================

class TestHappyPath:
    def test_dispatch_and_reap(self, db_engine):
        _clear_tables(db_engine)
        queue = RunQueue()
        queue.enqueue("NSE", "intraday")

        fake = FakePopen(pid=54321)
        argv_captured: list[list[str]] = []

        def popen_factory(argv):
            argv_captured.append(argv)
            return fake

        dispatcher = RunDispatcher(
            db_engine=db_engine, queue=queue,
            poll_interval_seconds=0.1,
            popen_factory=popen_factory,
        )
        dispatcher.start()

        # Wait for the spawn.
        _wait_until(lambda: len(argv_captured) >= 1, timeout=3)

        # argv shape check
        argv = argv_captured[0]
        assert "cycle" in argv
        assert "--exchange" in argv
        assert "NSE" in argv
        assert "--kind" in argv
        assert "intraday" in argv

        # Marker row present with our pid backfilled.
        _wait_until(
            lambda: _read_pid_for(db_engine, "NSE") == 54321,
            timeout=3,
        )

        # Simulate the subprocess exiting cleanly.
        fake._simulate_exit(rc=0)

        # Marker row must be gone.
        _wait_until(
            lambda: _read_pid_for(db_engine, "NSE") is None,
            timeout=3,
        )

        dispatcher.stop()
        dispatcher._thread.join(timeout=2)
        _clear_tables(db_engine)


# ============================================================
# cancel_current
# ============================================================

class TestCancelCurrent:
    def test_cancel_when_nothing_running_returns_false(
        self, db_engine,
    ):
        _clear_tables(db_engine)
        queue = RunQueue()
        dispatcher = RunDispatcher(
            db_engine=db_engine, queue=queue,
            poll_interval_seconds=0.1,
        )
        assert dispatcher.cancel_current() is False

    def test_cancel_sends_sigterm_and_subprocess_exits_gracefully(
        self, db_engine,
    ):
        _clear_tables(db_engine)
        queue = RunQueue()
        queue.enqueue("NSE", "intraday")

        fake = FakePopen()
        # SIGTERM handler: fake exits after receiving it.
        original_send = fake.send_signal
        def send_and_exit(sig):
            original_send(sig)
            fake._simulate_exit(rc=130)
        fake.send_signal = send_and_exit

        def popen_factory(argv):
            return fake

        dispatcher = RunDispatcher(
            db_engine=db_engine, queue=queue,
            poll_interval_seconds=0.1,
            sigterm_grace_seconds=1.0,
            popen_factory=popen_factory,
        )
        dispatcher.start()
        _wait_until(
            lambda: dispatcher.current_state().current_process is fake,
            timeout=3,
        )

        assert dispatcher.cancel_current() is True

        # SIGTERM was received; graceful exit was honored.
        import signal as sigmod
        assert sigmod.SIGTERM in fake.signals_received
        assert not fake.killed

        # Marker cleaned up.
        _wait_until(
            lambda: _read_pid_for(db_engine, "NSE") is None,
            timeout=3,
        )

        dispatcher.stop()
        dispatcher._thread.join(timeout=2)
        _clear_tables(db_engine)

    def test_cancel_escalates_to_sigkill_when_sigterm_ignored(
        self, db_engine,
    ):
        _clear_tables(db_engine)
        queue = RunQueue()
        queue.enqueue("NSE", "intraday")

        fake = FakePopen()
        # No exit-on-SIGTERM behavior; fake ignores the signal.

        def popen_factory(argv):
            return fake

        dispatcher = RunDispatcher(
            db_engine=db_engine, queue=queue,
            poll_interval_seconds=0.1,
            sigterm_grace_seconds=0.5,   # short grace for test speed
            popen_factory=popen_factory,
        )
        dispatcher.start()
        _wait_until(
            lambda: dispatcher.current_state().current_process is fake,
            timeout=3,
        )

        assert dispatcher.cancel_current() is True

        import signal as sigmod
        assert sigmod.SIGTERM in fake.signals_received
        assert fake.killed, "SIGKILL must have been called after grace"

        dispatcher.stop()
        dispatcher._thread.join(timeout=2)
        _clear_tables(db_engine)


# ============================================================
# Spawn failure cleanup
# ============================================================

class TestSpawnFailure:
    def test_popen_raising_cleans_up_marker(self, db_engine):
        _clear_tables(db_engine)
        queue = RunQueue()
        queue.enqueue("NSE", "intraday")

        def bad_popen(argv):
            raise OSError("simulated fork failure")

        dispatcher = RunDispatcher(
            db_engine=db_engine, queue=queue,
            poll_interval_seconds=0.1,
            popen_factory=bad_popen,
        )
        dispatcher.start()

        # Marker row was created, subprocess spawn failed, marker
        # should have been cleaned up in the except block.
        _wait_until(
            lambda: _read_pid_for(db_engine, "NSE") is None,
            timeout=3,
        )
        _wait_until(
            lambda: not _has_row(db_engine, "NSE"),
            timeout=3,
        )

        dispatcher.stop()
        dispatcher._thread.join(timeout=2)
        _clear_tables(db_engine)


# ============================================================
# Helpers
# ============================================================

def _read_pid_for(engine, exchange: str) -> int | None:
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT pid FROM update_run_in_progress "
                 "WHERE exchange = :ex"),
            {"ex": exchange},
        )
        row = result.first()
        return row.pid if row else None


def _has_row(engine, exchange: str) -> bool:
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT 1 FROM update_run_in_progress "
                 "WHERE exchange = :ex"),
            {"ex": exchange},
        )
        return result.first() is not None
