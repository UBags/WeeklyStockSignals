"""Tests for graceful shutdown via ASGI lifespan.

The production path is: uvicorn receives SIGTERM → uvicorn calls the
ASGI lifespan shutdown → our shutdown_callback runs → background threads
stop → DB engine disposes.

We can exercise the lifespan path without uvicorn by using FastAPI's
TestClient as a context manager — entering the context starts the
lifespan, exiting triggers shutdown. That's enough to verify our
callback is invoked exactly once and that exceptions inside it are
swallowed (so a buggy callback can't keep the process running).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.transports.http import create_app


class TestLifespanShutdown:

    def test_shutdown_callback_invoked_exactly_once(self):
        """Entering and exiting the TestClient context must call the
        shutdown callback exactly once."""
        cb = MagicMock(return_value=None)
        app = create_app(shutdown_callback=cb)
        with TestClient(app):
            # During context: lifespan startup ran; callback NOT called yet
            assert cb.call_count == 0
        # On context exit: lifespan shutdown ran; callback called once
        assert cb.call_count == 1

    def test_shutdown_callback_exception_does_not_propagate(self):
        """A buggy callback that raises must NOT prevent the lifespan
        from completing. Otherwise a bad shutdown hook could hang the
        process."""
        cb = MagicMock(side_effect=RuntimeError("oh no"))
        app = create_app(shutdown_callback=cb)
        # Should NOT raise — the lifespan handler swallows the exception
        # and logs it.
        with TestClient(app):
            pass
        # Verify the callback was indeed invoked
        assert cb.call_count == 1

    def test_no_shutdown_callback_app_still_works(self):
        """When called without a shutdown_callback, the app must still
        boot and shut down through the lifespan (or rather, skip the
        lifespan entirely) without errors."""
        app = create_app()
        # If this raised, the no-callback path is broken.
        with TestClient(app):
            pass


class TestShutdownCallbackOrdering:
    """The shutdown sequence built by _build_shutdown_callback in app.py
    must stop the distributor BEFORE the compute_scheduler, and dispose
    the db_engine LAST. We verify the order by tracking calls."""

    def test_shutdown_callback_orders_stops_correctly(self):
        from trip_ema.app import _build_shutdown_callback

        call_log: list[str] = []

        # Build a fake AppState with three observable stop targets
        state = MagicMock()
        state.distributor = MagicMock()
        state.distributor.stop.side_effect = lambda: call_log.append("distributor.stop")
        state.compute_scheduler = MagicMock()
        state.compute_scheduler.stop.side_effect = lambda: call_log.append("compute.stop")
        state.db_engine = MagicMock()
        state.db_engine.dispose.side_effect = lambda: call_log.append("db.dispose")

        shutdown = _build_shutdown_callback(state)
        shutdown()

        assert call_log == ["distributor.stop", "compute.stop", "db.dispose"]

    def test_shutdown_callback_continues_past_individual_failures(self):
        from trip_ema.app import _build_shutdown_callback

        # Distributor raises — but compute_scheduler.stop and
        # db_engine.dispose must STILL run.
        state = MagicMock()
        state.distributor = MagicMock()
        state.distributor.stop.side_effect = RuntimeError("distributor blew up")
        state.compute_scheduler = MagicMock()
        state.db_engine = MagicMock()

        shutdown = _build_shutdown_callback(state)
        shutdown()  # must not raise

        state.compute_scheduler.stop.assert_called_once()
        state.db_engine.dispose.assert_called_once()

    def test_shutdown_callback_handles_no_distributor(self):
        """In legacy/dev mode the distributor may be None; callback
        must not blow up."""
        from trip_ema.app import _build_shutdown_callback

        state = MagicMock()
        state.distributor = None
        state.compute_scheduler = MagicMock()
        state.db_engine = MagicMock()

        shutdown = _build_shutdown_callback(state)
        shutdown()

        state.compute_scheduler.stop.assert_called_once()
        state.db_engine.dispose.assert_called_once()

    def test_shutdown_callback_terminates_notifier_subprocess(self):
        from trip_ema.app import _build_shutdown_callback

        state = MagicMock()
        state.distributor = MagicMock()
        state.compute_scheduler = MagicMock()
        state.db_engine = MagicMock()

        proc = MagicMock()
        proc.is_alive.return_value = True
        proc.pid = 12345
        # First call: alive. After join, simulate it exited cleanly.
        proc.is_alive.side_effect = [True, False]

        shutdown = _build_shutdown_callback(state, notifier_proc=proc)
        shutdown()

        proc.terminate.assert_called_once()
        proc.join.assert_called_once()
        # Did NOT escalate to kill since join returned in time
        proc.kill.assert_not_called()

    def test_shutdown_callback_kills_notifier_if_terminate_times_out(self):
        from trip_ema.app import _build_shutdown_callback

        state = MagicMock()
        state.distributor = MagicMock()
        state.compute_scheduler = MagicMock()
        state.db_engine = MagicMock()

        proc = MagicMock()
        proc.pid = 12345
        # is_alive: True at entry, True still after join (timeout case)
        proc.is_alive.side_effect = [True, True]

        shutdown = _build_shutdown_callback(state, notifier_proc=proc)
        shutdown()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_shutdown_callback_uses_configured_timeout(self):
        """The notifier-subprocess join timeout must come from
        state.config.http.notifier_subprocess_shutdown_timeout_seconds
        (Phase 10 follow-up — was previously hardcoded to 15s)."""
        from trip_ema.app import _build_shutdown_callback

        state = MagicMock()
        state.distributor = MagicMock()
        state.compute_scheduler = MagicMock()
        state.db_engine = MagicMock()
        # Configure a non-default timeout
        state.config.http.notifier_subprocess_shutdown_timeout_seconds = 7

        proc = MagicMock()
        proc.pid = 12345
        proc.is_alive.side_effect = [True, False]

        shutdown = _build_shutdown_callback(state, notifier_proc=proc)
        shutdown()

        # join() was called with the configured timeout, not the old default 15
        proc.join.assert_called_once_with(timeout=7)
