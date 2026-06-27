"""Tests for `trip_ema.app._build_orchestrator_components`.

This is the wiring function that, given a Config plus the runtime
singletons, returns (PulseState | None, OrchestratorPool | None) per
the `scheduling.orchestrator.enabled` flag.

We don't exercise the full _build_app_state — that needs a real DB
and is covered by the integration test slice. These tests focus
strictly on the wiring contract.
"""

from __future__ import annotations

from datetime import time as dt_time
from unittest.mock import MagicMock

import pytest

from trip_ema.app import _build_orchestrator_components


def _market_hours_config(*, exchange: str = "NSE"):
    """A real MarketHoursConfig-like stand-in. We import the real
    pydantic model so to_tick_config / to_calendar_config behave
    correctly inside build_pool_from_configs."""
    from trip_ema.config.schema import MarketHoursConfig
    return MarketHoursConfig(
        exchange=exchange,
        tz="Asia/Kolkata",
        open="09:15",
        close="15:30",
        weekdays=[1, 2, 3, 4, 5],
    )


def _make_config(*, orchestrator_enabled: bool, markets: list | None = None):
    """Minimal Config-shaped MagicMock with just the attributes
    _build_orchestrator_components reads."""
    cfg = MagicMock()
    cfg.scheduling.orchestrator.enabled = orchestrator_enabled
    cfg.scheduling.orchestrator.fetch_timeout_seconds = 600
    cfg.scheduling.orchestrator.fetch_retry_delay_minutes = 5
    cfg.scheduling.orchestrator.catch_up_on_startup = False
    cfg.scheduling.orchestrator.catch_up_skip_if_recent_minutes = 30
    cfg.market_hours = markets or []
    cfg.etl.exchange = "NSE"
    # Step 10: _build_orchestrator_components calls
    # _make_v1_distribute_run_once(brevo_config=config.transports.brevo).
    # A default MagicMock here would be truthy → factory tries to
    # load a fake secrets file. None puts it on the no-op branch,
    # which is what these pool-construction tests want.
    cfg.transports.brevo = None
    return cfg


# ---------------------------------------------------------------------------
# Orchestrator-disabled mode
# ---------------------------------------------------------------------------


class TestOrchestratorDisabled:
    def test_returns_none_pair_when_disabled(self):
        cfg = _make_config(orchestrator_enabled=False)
        ps, pool = _build_orchestrator_components(
            config=cfg,
            db_engine=MagicMock(),
            compute_scheduler=MagicMock(),
            distributor=MagicMock(),
            v1_cache=MagicMock(),
        )
        assert ps is None
        assert pool is None

    def test_disabled_path_does_not_touch_market_hours(self):
        # Even with bogus market_hours, disabled mode shouldn't blow up.
        cfg = _make_config(orchestrator_enabled=False, markets=["junk"])
        ps, pool = _build_orchestrator_components(
            config=cfg,
            db_engine=MagicMock(),
            compute_scheduler=MagicMock(),
            distributor=MagicMock(),
            v1_cache=MagicMock(),
        )
        assert ps is None and pool is None


# ---------------------------------------------------------------------------
# Orchestrator-enabled mode
# ---------------------------------------------------------------------------


class TestOrchestratorEnabled:
    def test_returns_pulse_state_and_pool(self):
        cfg = _make_config(
            orchestrator_enabled=True,
            markets=[_market_hours_config(exchange="NSE")],
        )
        ps, pool = _build_orchestrator_components(
            config=cfg,
            db_engine=MagicMock(),
            compute_scheduler=MagicMock(),
            distributor=MagicMock(),
            v1_cache=MagicMock(),
        )
        assert ps is not None
        assert pool is not None
        assert pool.exchanges() == ["NSE"]

    def test_multiple_markets(self):
        cfg = _make_config(
            orchestrator_enabled=True,
            markets=[
                _market_hours_config(exchange="NSE"),
                _market_hours_config(exchange="BSE"),
            ],
        )
        ps, pool = _build_orchestrator_components(
            config=cfg,
            db_engine=MagicMock(),
            compute_scheduler=MagicMock(),
            distributor=MagicMock(),
            v1_cache=MagicMock(),
        )
        assert pool.exchanges() == ["BSE", "NSE"]

    def test_empty_market_hours_returns_empty_pool(self):
        """Enabled but no markets configured: returns a pool with
        zero orchestrators. Service still boots; admin endpoints
        report pool_size=0 rather than failing."""
        cfg = _make_config(orchestrator_enabled=True, markets=[])
        ps, pool = _build_orchestrator_components(
            config=cfg,
            db_engine=MagicMock(),
            compute_scheduler=MagicMock(),
            distributor=MagicMock(),
            v1_cache=MagicMock(),
        )
        assert ps is not None
        assert pool is not None
        assert len(pool) == 0

    def test_pool_has_pulse_hook_wired(self):
        """The pool's pulse_hook should be the same PulseState
        returned — so orchestrator_started events flow through
        the public publish path."""
        cfg = _make_config(
            orchestrator_enabled=True,
            markets=[_market_hours_config(exchange="NSE")],
        )
        ps, pool = _build_orchestrator_components(
            config=cfg,
            db_engine=MagicMock(),
            compute_scheduler=MagicMock(),
            distributor=MagicMock(),
            v1_cache=MagicMock(),
        )
        # The pool's internal pulse_hook is what receives
        # orchestrator_started/stopped events. Calling start_all
        # would invoke real orchestrator threads (we don't want
        # that here); instead we directly check that the orchestrator
        # instances have the same pulse object.
        for orch in pool.all():
            assert orch._pulse is ps  # noqa: access private

    def test_orchestrator_settings_derived_from_config(self):
        """The OrchestratorSettings values should match what was
        configured. Verifies the timedelta conversions."""
        from datetime import timedelta
        cfg = _make_config(
            orchestrator_enabled=True,
            markets=[_market_hours_config(exchange="NSE")],
        )
        # Override the settings explicitly.
        cfg.scheduling.orchestrator.fetch_timeout_seconds = 120
        cfg.scheduling.orchestrator.fetch_retry_delay_minutes = 7
        cfg.scheduling.orchestrator.catch_up_on_startup = True
        cfg.scheduling.orchestrator.catch_up_skip_if_recent_minutes = 45

        ps, pool = _build_orchestrator_components(
            config=cfg,
            db_engine=MagicMock(),
            compute_scheduler=MagicMock(),
            distributor=MagicMock(),
            v1_cache=MagicMock(),
        )
        orch = pool.get("NSE")
        s = orch._settings  # noqa: access private
        assert s.fetch_timeout == timedelta(seconds=120)
        assert s.fetch_retry_delay == timedelta(minutes=7)
        assert s.catch_up_on_startup is True
        assert s.catch_up_skip_if_recent == timedelta(minutes=45)
