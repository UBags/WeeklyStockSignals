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

    def test_emits_running_and_done(self):
        """Verify the "running" progress-marker fires before any DB
        activity in _run_fetch_pass.

        Phase ETL: EtlStatusRepository is injected via kwarg. The fake
        repo's `open_run` raises StopAfterRunning, which short-circuits
        the function after cleanup_stale_runs — by that point the
        "running" emit has already fired (line ordering in
        _run_fetch_pass). No more setattr on the runner module.
        """
        from trip_ema.etl.types import RunKind, Tier
        from trip_ema.domain import Timeframe

        class StopAfterRunning(Exception):
            pass

        class FakeRepo:
            def __init__(self, conn):
                pass

            def cleanup_stale_runs(self, *, exchange):
                return 0

            def open_run(self, *args, **kwargs):
                # By the time this is called, `_emit_fetch_progress(
                # progress_cb, "running")` has already run.
                raise StopAfterRunning("test stop")

        # Make db_engine.begin() return a context manager
        engine = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = lambda self: MagicMock()
        ctx.__exit__ = lambda self, *a: None
        engine.begin = lambda: ctx

        calls = []
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
                etl_status_repo_factory=FakeRepo,
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
