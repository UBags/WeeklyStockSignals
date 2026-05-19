"""Tests for the schema additions in Step 2.

Covers:
  - MarketHoursConfig with new optional fields (validators + defaults)
  - to_tick_config / to_calendar_config adapter methods
  - OrchestratorConfig defaults and validation bounds
  - Backward compatibility: existing-shaped YAML still parses
"""

from __future__ import annotations

from datetime import time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from trip_ema.config.schema import (
    MarketHoursConfig,
    OrchestratorConfig,
    SchedulingConfig,
)


# ---------------------------------------------------------------------------
# MarketHoursConfig
# ---------------------------------------------------------------------------


class TestMarketHoursConfigBackwardCompat:
    """A YAML file written before Step 2 (no new fields) must keep parsing."""

    def test_legacy_shape_parses(self):
        # Only the original required fields. No new fields supplied.
        cfg = MarketHoursConfig(
            exchange="NSE",
            open="09:15",
            close="15:30",
            tz="Asia/Kolkata",
            weekdays=[1, 2, 3, 4, 5],
        )
        # Defaults populated.
        assert cfg.post_open_offset_minutes == 5
        assert cfg.pre_close_offset_minutes == 15
        assert cfg.intraday_cadence_minutes == 60
        assert cfg.holidays_country is None
        assert cfg.holidays_override_file is None


class TestMarketHoursConfigValidators:
    def test_invalid_open_string_rejected(self):
        with pytest.raises(ValidationError, match="HH:MM"):
            MarketHoursConfig(
                exchange="X", open="9-15", close="15:30",
                tz="Asia/Kolkata", weekdays=[1, 2, 3, 4, 5],
            )

    def test_invalid_close_string_rejected(self):
        with pytest.raises(ValidationError, match="HH:MM"):
            MarketHoursConfig(
                exchange="X", open="09:15", close="25:00",
                tz="Asia/Kolkata", weekdays=[1, 2, 3, 4, 5],
            )

    def test_unknown_tz_rejected_at_startup(self):
        # Catching this at config-load time is the whole point — much
        # better than discovering it at first tick.
        with pytest.raises(ValidationError, match="unknown IANA tz"):
            MarketHoursConfig(
                exchange="X", open="09:15", close="15:30",
                tz="Asia/Bengaluru",  # not a real IANA name
                weekdays=[1, 2, 3, 4, 5],
            )

    def test_weekday_range_enforced(self):
        with pytest.raises(ValidationError, match="weekdays must be 1..7"):
            MarketHoursConfig(
                exchange="X", open="09:15", close="15:30",
                tz="UTC", weekdays=[0, 1, 2],
            )

    def test_extra_field_rejected(self):
        # extra="forbid" guards against typos in YAML keys.
        with pytest.raises(ValidationError, match="extra"):
            MarketHoursConfig(
                exchange="X", open="09:15", close="15:30",
                tz="UTC", weekdays=[1, 2, 3, 4, 5],
                pos_open_offset_minutes=7,  # typo: pos vs post
            )

    def test_post_open_offset_bounds(self):
        # Upper bound 240 (4 hours after open) — beyond that is
        # almost certainly a misconfiguration.
        with pytest.raises(ValidationError):
            MarketHoursConfig(
                exchange="X", open="09:15", close="15:30",
                tz="UTC", weekdays=[1, 2, 3, 4, 5],
                post_open_offset_minutes=500,
            )

    def test_intraday_cadence_must_be_positive(self):
        with pytest.raises(ValidationError):
            MarketHoursConfig(
                exchange="X", open="09:15", close="15:30",
                tz="UTC", weekdays=[1, 2, 3, 4, 5],
                intraday_cadence_minutes=0,
            )


class TestMarketHoursConfigAdapters:
    @pytest.fixture
    def cfg(self) -> MarketHoursConfig:
        return MarketHoursConfig(
            exchange="NSE",
            open="09:15",
            close="15:30",
            tz="Asia/Kolkata",
            weekdays=[1, 2, 3, 4, 5],
            post_open_offset_minutes=5,
            pre_close_offset_minutes=15,
            intraday_cadence_minutes=60,
            holidays_country="IN",
        )

    def test_to_tick_config_types(self, cfg):
        tick = cfg.to_tick_config()
        assert tick.exchange == "NSE"
        assert tick.tz == ZoneInfo("Asia/Kolkata")
        assert tick.open == dt_time(9, 15)
        assert tick.close == dt_time(15, 30)
        assert tick.post_open_offset == timedelta(minutes=5)
        assert tick.pre_close_offset == timedelta(minutes=15)
        assert tick.intraday_cadence == timedelta(hours=1)

    def test_to_tick_config_is_pure(self, cfg):
        # Adapter must be a pure function — repeated calls yield equal
        # values (and equal-by-hash since the dataclass is frozen).
        a = cfg.to_tick_config()
        b = cfg.to_tick_config()
        assert a == b
        assert hash(a) == hash(b)

    def test_to_calendar_config_with_country(self, cfg):
        cal_cfg = cfg.to_calendar_config()
        assert cal_cfg.exchange == "NSE"
        assert cal_cfg.weekdays == frozenset([1, 2, 3, 4, 5])
        assert cal_cfg.holidays_country == "IN"
        assert cal_cfg.holidays_override_file is None

    def test_to_calendar_config_without_country(self):
        # holidays_country defaults to None when not supplied; the
        # adapter should propagate that.
        cfg = MarketHoursConfig(
            exchange="X", open="09:00", close="17:00",
            tz="UTC", weekdays=[1, 2, 3, 4, 5],
        )
        cal_cfg = cfg.to_calendar_config()
        assert cal_cfg.holidays_country is None

    def test_to_calendar_config_with_override_file(self):
        cfg = MarketHoursConfig(
            exchange="X", open="09:00", close="17:00",
            tz="UTC", weekdays=[1, 2, 3, 4, 5],
            holidays_override_file="configs/x_holidays.yaml",
        )
        cal_cfg = cfg.to_calendar_config()
        assert cal_cfg.holidays_override_file == Path("configs/x_holidays.yaml")


# ---------------------------------------------------------------------------
# OrchestratorConfig
# ---------------------------------------------------------------------------


class TestOrchestratorConfig:
    def test_all_defaults(self):
        cfg = OrchestratorConfig()
        assert cfg.enabled is False  # default off during rollout
        assert cfg.catch_up_on_startup is False
        assert cfg.catch_up_skip_if_recent_minutes == 30
        assert cfg.fetch_timeout_seconds == 600
        assert cfg.fetch_retry_delay_minutes == 5

    def test_can_override_all_fields(self):
        cfg = OrchestratorConfig(
            enabled=True,
            catch_up_on_startup=True,
            catch_up_skip_if_recent_minutes=60,
            fetch_timeout_seconds=300,
            fetch_retry_delay_minutes=10,
        )
        assert cfg.enabled is True
        assert cfg.fetch_timeout_seconds == 300

    def test_fetch_timeout_lower_bound(self):
        with pytest.raises(ValidationError):
            OrchestratorConfig(fetch_timeout_seconds=10)

    def test_fetch_timeout_upper_bound(self):
        with pytest.raises(ValidationError):
            OrchestratorConfig(fetch_timeout_seconds=99999)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError, match="extra"):
            OrchestratorConfig(typo_field=True)


# ---------------------------------------------------------------------------
# SchedulingConfig integration
# ---------------------------------------------------------------------------


class TestSchedulingConfigIntegration:
    def test_legacy_scheduling_yaml_still_parses(self):
        # YAML written before Step 2 — no `orchestrator` block.
        cfg = SchedulingConfig(
            compute_cadence_minutes=60,
            subscriber_cadence_minutes=15,
        )
        # orchestrator block defaulted with enabled=False.
        assert cfg.orchestrator.enabled is False

    def test_nested_orchestrator_block(self):
        cfg = SchedulingConfig(
            compute_cadence_minutes=60,
            subscriber_cadence_minutes=15,
            orchestrator=OrchestratorConfig(enabled=True),
        )
        assert cfg.orchestrator.enabled is True

    def test_can_construct_with_orchestrator_as_dict(self):
        # Pydantic handles dict → nested model coercion, so the YAML
        # loader doesn't have to construct nested models explicitly.
        cfg = SchedulingConfig(
            compute_cadence_minutes=60,
            subscriber_cadence_minutes=15,
            orchestrator={"enabled": True, "fetch_timeout_seconds": 300},
        )
        assert cfg.orchestrator.enabled is True
        assert cfg.orchestrator.fetch_timeout_seconds == 300
        # Other orchestrator fields take their defaults.
        assert cfg.orchestrator.catch_up_on_startup is False
