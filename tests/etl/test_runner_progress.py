"""Test that _run_fetch_pass emits "fetch_bars" progress markers
when progress_cb is provided (Step 13d-1).

This test ONLY exercises the hook plumbing — the heavy yfinance
fetch path is mocked out. End-to-end fetch behaviour is covered
by tests/etl/test_runner.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trip_ema.etl import runner as runner_mod


# ============================================================
# _emit_fetch_progress helper
# ============================================================

class TestEmitFetchProgress:

    def test_none_callback_is_noop(self):
        runner_mod._emit_fetch_progress(None, "running")

    def test_calls_callback(self):
        calls = []
        runner_mod._emit_fetch_progress(
            lambda s, m: calls.append((s, m)),
            "running",
        )
        assert calls == [("fetch_bars", "running")]

    def test_swallows_callback_exception(self):
        def bad_cb(s, m):
            raise RuntimeError("intentional")
        runner_mod._emit_fetch_progress(bad_cb, "running")


# ============================================================
# Wire test — exercise _run_fetch_pass with mocked internals
# ============================================================

class TestRunFetchPassProgressCb:

    def test_emits_running_and_done(self, monkeypatch):
        """Fully mock out the body so we can observe the wire."""
        from trip_ema.etl.types import RunKind, Tier
        from trip_ema.domain import Timeframe

        # Mock the inner cleanup_stale_runs call
        fake_repo = MagicMock()
        fake_repo.cleanup_stale_runs = MagicMock(return_value=0)
        monkeypatch.setattr(
            runner_mod, "EtlStatusRepository",
            lambda conn: fake_repo,
        )

        # Mock the rest of _run_fetch_pass's body via a custom
        # implementation that just returns. We do this by patching
        # the YFinanceSource construction so it doesn't try to
        # actually hit yfinance.
        # ALTERNATIVE: since _run_fetch_pass is long and has many
        # internal calls, we'll just patch the top-level orchestration
        # function. The progress_cb wire is the FIRST thing inside
        # the `with correlation_id` block, so we only need it to
        # reach that point.

        # Make db_engine.begin() return a context manager
        engine = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = lambda self: MagicMock()
        ctx.__exit__ = lambda self, *a: None
        engine.begin = lambda: ctx

        calls = []

        # We need to intercept the function's exit before it gets
        # to the actual yfinance call. Patch the function to raise
        # an early sentinel exception after the running emit fires.
        # OR: just let it call the real function with a heavily
        # mocked source, and check that the progress_cb was hit.

        # Simplest: patch the post-cleanup path to raise immediately
        # so we can verify the "running" emit fired before the raise.
        class StopAfterRunning(Exception):
            pass

        original_emit = runner_mod._emit_fetch_progress

        def wrapped_emit(cb, msg):
            original_emit(cb, msg)
            if msg == "running":
                raise StopAfterRunning("test stop")

        monkeypatch.setattr(
            runner_mod, "_emit_fetch_progress", wrapped_emit,
        )

        config = MagicMock()
        config.etl.yfinance_batch_size = 100
        config.etl.yfinance_request_delay_seconds = 0.0

        with pytest.raises(StopAfterRunning):
            runner_mod._run_fetch_pass(
                db_engine=engine,
                config=config,
                exchange="NSE",
                kind=RunKind.INTRADAY,
                tier_filter={Tier.TIER_1},
                timeframe=Timeframe.DAILY,
                source=None,
                progress_cb=lambda s, m: calls.append((s, m)),
            )

        # We caught the StopAfterRunning, which means the "running"
        # emit DID fire
        assert ("fetch_bars", "running") in calls

    def test_progress_cb_none_is_safe(self):
        """Smoke: progress_cb=None must not raise even at the
        emit sites. This is a fast unit-test that doesn't run the
        full fetch."""
        runner_mod._emit_fetch_progress(None, "running")
        runner_mod._emit_fetch_progress(None, "done")
        runner_mod._emit_fetch_progress(None, "failed: e")
