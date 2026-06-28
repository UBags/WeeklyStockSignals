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


# === STEP 14B-FIXUP7: TestShutdownCallbackOrdering class removed ===
# All 6 tests in that class were built around the legacy
# distributor.stop + compute_scheduler.stop call ordering and
# the notifier_proc subprocess termination — both demolished
# in V1 cutover Step 14b.
