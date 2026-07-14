"""Test that notify_subscribers emits "notify" progress markers
when progress_cb is provided (Step 13d-1).

Light test — heavy paths (compose + Brevo) are covered elsewhere.
We only verify the cb fires "running" and then either "done" or
"done: skipped (...)" depending on the path taken.

NOTE on imports: `trip_ema.orchestrator.__init__.py` re-exports
`notify_subscribers` (the function), which SHADOWS the submodule
of the same name. importlib.import_module bypasses the shadow.
"""
from __future__ import annotations

import importlib
from datetime import datetime, UTC
from unittest.mock import MagicMock

import pytest

notify_mod = importlib.import_module(
    "trip_ema.orchestrator.notify_subscribers",
)
notify_subscribers = notify_mod.notify_subscribers


# ============================================================
# _emit_notify_progress helper
# ============================================================

class TestEmitNotifyProgress:

    def test_none_callback_is_noop(self):
        notify_mod._emit_notify_progress(None, "running")

    def test_calls_callback(self):
        calls = []
        notify_mod._emit_notify_progress(
            lambda s, m: calls.append((s, m)),
            "running",
        )
        assert calls == [("notify", "running")]

    def test_swallows_callback_exception(self):
        def bad_cb(s, m):
            raise RuntimeError("intentional")
        notify_mod._emit_notify_progress(bad_cb, "running")


# ============================================================
# Wire test — early-exit path (no signals)
# ============================================================

class TestNotifyProgressNoSignals:

    def test_emits_skipped_when_no_signals(self):
        """When current_signals is empty, the function returns
        early with NotifyResult.signals_in_current=0. The progress
        cb should still see "running" then "done: skipped...".
        """
        repo = MagicMock()
        repo.count = MagicMock(return_value=0)

        engine = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = lambda self: MagicMock()
        ctx.__exit__ = lambda self, *a: None
        engine.connect = lambda: ctx

        calls = []
        result = notify_subscribers(
            engine,
            brevo_api_key="xkeysib-fake",
            brevo_from_address="from@example.com",
            cycle_id="test-cycle",
            now=datetime(2026, 6, 28, 12, 0, tzinfo=UTC),
            progress_cb=lambda s, m: calls.append((s, m)),
            signals_repo_factory=lambda conn: repo,
        )

        assert calls[0] == ("notify", "running")
        assert calls[-1][0] == "notify"
        assert calls[-1][1].startswith("done: skipped")
        assert result.signals_in_current == 0


class TestNotifyProgressBackwardCompat:

    def test_no_progress_cb_is_safe(self):
        """Default path (no progress_cb) still works."""
        repo = MagicMock()
        repo.count = MagicMock(return_value=0)
        engine = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = lambda self: MagicMock()
        ctx.__exit__ = lambda self, *a: None
        engine.connect = lambda: ctx

        result = notify_subscribers(
            engine,
            brevo_api_key="xkeysib-fake",
            brevo_from_address="from@example.com",
            cycle_id="test-cycle",
            now=datetime(2026, 6, 28, 12, 0, tzinfo=UTC),
            signals_repo_factory=lambda conn: repo,
        )
        assert result.signals_in_current == 0
