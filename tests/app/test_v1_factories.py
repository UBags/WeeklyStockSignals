"""Unit tests for the V1 factory functions added to `trip_ema.app`
in Step 10 of the V1 cutover.

The factories are pure construction logic (they close over args and
return callables). The tests verify:
  - compute_run_once calls recompute_market with correct args
  - distribute_run_once with Brevo configured calls notify_subscribers
    with correct args; cycle_id is generated per call from the wall
    clock
  - distribute_run_once with Brevo NOT configured returns None and
    does NOT call notify_subscribers

The factory functions are private (`_make_v1_*`) but stable for
testing — `_build_orchestrator_components` uses them too. The
sibling test file `test_build_orchestrator_components.py` covers
their integration into the pool.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest


# ============================================================
# _make_v1_compute_run_once
# ============================================================

class TestMakeV1ComputeRunOnce:

    def test_returns_callable(self):
        from trip_ema.app import _make_v1_compute_run_once
        v1_cache = MagicMock()
        run = _make_v1_compute_run_once(
            db_engine=MagicMock(), v1_cache=v1_cache,
        )
        assert callable(run)

    def test_invokes_recompute_market_with_nse_default(self):
        from trip_ema.app import _make_v1_compute_run_once
        db_engine = MagicMock()
        v1_cache = MagicMock()
        sentinel = MagicMock()

        with patch(
            "trip_ema.app.recompute_market",
            return_value=sentinel,
        ) as rm:
            run = _make_v1_compute_run_once(
                db_engine=db_engine, v1_cache=v1_cache,
            )
            result = run()

        assert result is sentinel
        rm.assert_called_once_with(
            db_engine,
            mode="live",
            cache=v1_cache,
            exchange="NSE",
        )

    def test_exchange_override(self):
        """Other callers (e.g. backfill in Step 11) can override the
        exchange to run a single non-NSE market through the same
        pipeline."""
        from trip_ema.app import _make_v1_compute_run_once
        db_engine = MagicMock()
        v1_cache = MagicMock()

        with patch("trip_ema.app.recompute_market") as rm:
            run = _make_v1_compute_run_once(
                db_engine=db_engine, v1_cache=v1_cache,
                exchange="BSE",
            )
            run()

        kwargs = rm.call_args.kwargs
        assert kwargs["exchange"] == "BSE"

    def test_each_invocation_calls_recompute_market_again(self):
        """Sanity: the factory returns a callable that re-invokes
        on every call (no result caching at the factory layer)."""
        from trip_ema.app import _make_v1_compute_run_once
        v1_cache = MagicMock()

        with patch("trip_ema.app.recompute_market") as rm:
            run = _make_v1_compute_run_once(
                db_engine=MagicMock(), v1_cache=v1_cache,
            )
            for _ in range(3):
                run()
        assert rm.call_count == 3


# ============================================================
# _make_v1_distribute_run_once
# ============================================================

class TestMakeV1DistributeRunOnceConfigured:

    def _brevo_cfg(self, secrets_file="secrets/brevo.yaml"):
        cfg = MagicMock()
        cfg.secrets_file = secrets_file
        cfg.sender_email = "weeklystocksignals@outlook.com"
        cfg.sender_name = "WeeklyStockSignals"
        return cfg

    def test_returns_callable(self):
        from trip_ema.app import _make_v1_distribute_run_once
        with patch(
            "trip_ema.config.brevo_secrets.load_brevo_api_key",
            return_value="xkeysib-test",
        ):
            run = _make_v1_distribute_run_once(
                db_engine=MagicMock(),
                brevo_config=self._brevo_cfg(),
            )
        assert callable(run)

    def test_invokes_notify_subscribers_with_brevo_args(self):
        from trip_ema.app import _make_v1_distribute_run_once
        db_engine = MagicMock()
        sentinel = MagicMock()

        with patch(
            "trip_ema.config.brevo_secrets.load_brevo_api_key",
            return_value="xkeysib-loaded-from-secrets",
        ), patch(
            "trip_ema.app.notify_subscribers",
            return_value=sentinel,
        ) as ns:
            run = _make_v1_distribute_run_once(
                db_engine=db_engine,
                brevo_config=self._brevo_cfg(),
            )
            result = run()

        assert result is sentinel
        assert ns.call_count == 1
        kwargs = ns.call_args.kwargs
        assert kwargs["brevo_api_key"] == "xkeysib-loaded-from-secrets"
        assert kwargs["brevo_from_address"] == (
            "weeklystocksignals@outlook.com"
        )
        assert kwargs["brevo_from_name"] == "WeeklyStockSignals"
        # positional: db_engine
        assert ns.call_args.args == (db_engine,)

    def test_cycle_id_is_yyyymmdd_hhmm_utc(self):
        from trip_ema.app import _make_v1_distribute_run_once
        with patch(
            "trip_ema.config.brevo_secrets.load_brevo_api_key",
            return_value="xkeysib-test",
        ), patch(
            "trip_ema.app.notify_subscribers",
        ) as ns:
            run = _make_v1_distribute_run_once(
                db_engine=MagicMock(),
                brevo_config=self._brevo_cfg(),
            )
            run()

        cycle_id = ns.call_args.kwargs["cycle_id"]
        assert isinstance(cycle_id, str)
        # Format: YYYYMMDD-HHMM (8 + 1 + 4 = 13 chars)
        assert re.fullmatch(r"\d{8}-\d{4}", cycle_id), (
            f"cycle_id {cycle_id!r} doesn't match YYYYMMDD-HHMM"
        )

    def test_now_is_utc(self):
        from trip_ema.app import _make_v1_distribute_run_once
        with patch(
            "trip_ema.config.brevo_secrets.load_brevo_api_key",
            return_value="xkeysib-test",
        ), patch(
            "trip_ema.app.notify_subscribers",
        ) as ns:
            run = _make_v1_distribute_run_once(
                db_engine=MagicMock(),
                brevo_config=self._brevo_cfg(),
            )
            run()

        now_arg = ns.call_args.kwargs["now"]
        assert isinstance(now_arg, datetime)
        assert now_arg.tzinfo is UTC

    def test_each_invocation_generates_fresh_now_and_cycle_id(self):
        """The closure must NOT cache the cycle_id at construction
        time — that would give every tick the same identifier."""
        from trip_ema.app import _make_v1_distribute_run_once
        with patch(
            "trip_ema.config.brevo_secrets.load_brevo_api_key",
            return_value="xkeysib-test",
        ), patch(
            "trip_ema.app.notify_subscribers",
        ) as ns:
            run = _make_v1_distribute_run_once(
                db_engine=MagicMock(),
                brevo_config=self._brevo_cfg(),
            )
            # Two ticks, far enough apart that the minute may differ.
            # Even if they're in the same minute, two distinct `now`
            # datetimes still arrive — `datetime.now()` advances at
            # microsecond resolution.
            import time
            run()
            time.sleep(0.001)
            run()

        # cycle_id may or may not differ (depends on whether the
        # clock crossed a minute boundary). But the `now` argument
        # MUST differ between the two calls.
        nows = [c.kwargs["now"] for c in ns.call_args_list]
        assert len(nows) == 2
        assert nows[0] != nows[1]


class TestMakeV1DistributeRunOnceUnconfigured:

    def test_returns_noop_when_brevo_config_is_none(self):
        from trip_ema.app import _make_v1_distribute_run_once
        run = _make_v1_distribute_run_once(
            db_engine=MagicMock(),
            brevo_config=None,
        )
        assert callable(run)

    def test_noop_returns_none_without_calling_notify_subscribers(
        self,
    ):
        from trip_ema.app import _make_v1_distribute_run_once
        with patch(
            "trip_ema.app.notify_subscribers",
        ) as ns, patch(
            "trip_ema.config.brevo_secrets.load_brevo_api_key",
        ) as loader:
            run = _make_v1_distribute_run_once(
                db_engine=MagicMock(),
                brevo_config=None,
            )
            result = run()

        assert result is None
        ns.assert_not_called()
        loader.assert_not_called()

    def test_noop_can_be_invoked_many_times(self):
        """The orchestrator calls distribute_run_once every tick. The
        no-op variant must be safe to call indefinitely."""
        from trip_ema.app import _make_v1_distribute_run_once
        run = _make_v1_distribute_run_once(
            db_engine=MagicMock(),
            brevo_config=None,
        )
        for _ in range(100):
            assert run() is None
