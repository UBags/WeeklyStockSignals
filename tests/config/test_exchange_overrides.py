"""Tests for the per-(exchange, timeframe) indicator parameter
overrides introduced in Phase 15a.

Two surfaces:

  1. `Config.exchange_overrides` field — validation that bad
     exchange or bad timeframe keys are rejected at config-load
     time. The validator lives in `Config._validate_exchange_overrides`
     and the `ConfigError` wraps Pydantic's ValidationError.

  2. `resolve_indicator_params(config, exchange, timeframe)` —
     returns the override's IndicatorParams when present, falls
     back to `config.to_indicator_params()` otherwise. This is the
     function the signal generator calls per (ticker, timeframe).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from trip_ema.config import Config, ConfigError, load_config
from trip_ema.config.exchange_overrides import (
    SUPPORTED_BACKTEST_TIMEFRAMES,
    resolve_indicator_params,
)
from trip_ema.domain import Timeframe


# ---------- YAML scaffolding ---------- #


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return p


# Base YAML with NSE and NYSE configured; no exchange_overrides yet.
_BASE_YAML = """
database:
  driver: postgresql
  host: localhost
  port: 5432
  database: market_data
  user: app
  password_env: TEST_DB_PWD
ema:
  med:     { base: 8,  s1: 4, s2: 4, final: null }
  long:    { base: 15, s1: 4, s2: 4, final: 3 }
  longest: { base: 28, s1: 7, s2: 5, final: 3 }
atr:
  length: 14
  inner_mult: 0.2
  mid_mult: 2.0
  outer_mult: 4.0
market_hours:
  - exchange: NSE
    open: "09:15"
    close: "15:30"
    tz: Asia/Kolkata
    weekdays: [1, 2, 3, 4, 5]
  - exchange: NYSE
    open: "09:15"
    close: "15:30"
    tz: Asia/Kolkata
    weekdays: [1, 2, 3, 4, 5]
"""


# A YAML snippet to add NSE 1W override with bumped params.
_NSE_1W_OVERRIDE = """
exchange_overrides:
  NSE:
    "1W":
      ema:
        med:     { base: 10, s1: 5, s2: 5 }
        long:    { base: 20, s1: 5, s2: 5, final: 4 }
        longest: { base: 50, s1: 10, s2: 8, final: 4 }
        volume_sma_length: 3
      atr:
        length: 21
        inner_mult: 0.3
        mid_mult: 2.5
        outer_mult: 5.0
"""


@pytest.fixture
def db_pwd(monkeypatch):
    monkeypatch.setenv("TEST_DB_PWD", "secret")


# ---------- Backward compatibility ---------- #


class TestExchangeOverridesBackCompat:
    def test_config_without_overrides_loads_with_field_None(
        self, tmp_path, db_pwd,
    ):
        """Existing configs with no exchange_overrides: block keep
        working. The Config.exchange_overrides field defaults to None."""
        p = _write(tmp_path, _BASE_YAML)
        cfg = load_config(p)
        assert cfg.exchange_overrides is None

    def test_resolve_indicator_params_no_overrides_returns_global(
        self, tmp_path, db_pwd,
    ):
        """When exchange_overrides is None, resolve_indicator_params
        returns the global params from to_indicator_params()."""
        p = _write(tmp_path, _BASE_YAML)
        cfg = load_config(p)
        params = resolve_indicator_params(cfg, "NSE", Timeframe.WEEKLY)
        expected = cfg.to_indicator_params()
        assert params == expected


# ---------- Resolution logic ---------- #


class TestResolveIndicatorParams:
    def test_override_present_returns_override_values(
        self, tmp_path, db_pwd,
    ):
        """NSE 1W is overridden; resolver returns the override."""
        p = _write(tmp_path, _BASE_YAML + _NSE_1W_OVERRIDE)
        cfg = load_config(p)
        params = resolve_indicator_params(cfg, "NSE", Timeframe.WEEKLY)
        assert params.med_base == 10        # overridden
        assert params.longest_base == 50    # overridden
        assert params.atr_length == 21      # overridden
        assert params.volume_sma_length == 3
        # IndicatorParams's behavior flag isn't an override field;
        # it stays at its dataclass default.
        assert params.goldilocks_corridor_on_fresh_entry_only is True

    def test_same_exchange_other_timeframe_falls_back_to_global(
        self, tmp_path, db_pwd,
    ):
        """NSE has a 1W override but no 1D override → NSE 1D falls
        back to the global block."""
        p = _write(tmp_path, _BASE_YAML + _NSE_1W_OVERRIDE)
        cfg = load_config(p)
        nse_daily = resolve_indicator_params(cfg, "NSE", Timeframe.DAILY)
        assert nse_daily == cfg.to_indicator_params()

    def test_other_exchange_falls_back_to_global(self, tmp_path, db_pwd):
        """NYSE has no override at all → NYSE 1W falls back."""
        p = _write(tmp_path, _BASE_YAML + _NSE_1W_OVERRIDE)
        cfg = load_config(p)
        nyse_weekly = resolve_indicator_params(cfg, "NYSE", Timeframe.WEEKLY)
        assert nyse_weekly == cfg.to_indicator_params()

    def test_per_timeframe_independence_within_one_exchange(
        self, tmp_path, db_pwd,
    ):
        """An exchange can have one timeframe overridden and another
        falling back. Per design D4."""
        # NSE 1W overridden via _NSE_1W_OVERRIDE; NSE 1D not overridden.
        p = _write(tmp_path, _BASE_YAML + _NSE_1W_OVERRIDE)
        cfg = load_config(p)
        weekly = resolve_indicator_params(cfg, "NSE", Timeframe.WEEKLY)
        daily = resolve_indicator_params(cfg, "NSE", Timeframe.DAILY)
        assert weekly.med_base == 10       # override
        assert daily.med_base == 8         # global
        assert weekly != daily

    def test_two_exchanges_with_distinct_overrides(self, tmp_path, db_pwd):
        """Each exchange's per-timeframe overrides are independent."""
        yaml = _BASE_YAML + """
exchange_overrides:
  NSE:
    "1W":
      ema:
        med:     { base: 10, s1: 5, s2: 5 }
        long:    { base: 20, s1: 5, s2: 5, final: 4 }
        longest: { base: 50, s1: 10, s2: 8, final: 4 }
        volume_sma_length: 3
      atr:
        length: 21
        inner_mult: 0.3
        mid_mult: 2.5
        outer_mult: 5.0
  NYSE:
    "1W":
      ema:
        med:     { base: 12, s1: 6, s2: 6 }
        long:    { base: 25, s1: 6, s2: 6, final: 5 }
        longest: { base: 60, s1: 12, s2: 9, final: 5 }
        volume_sma_length: 4
      atr:
        length: 28
        inner_mult: 0.4
        mid_mult: 3.0
        outer_mult: 6.0
"""
        p = _write(tmp_path, yaml)
        cfg = load_config(p)
        nse = resolve_indicator_params(cfg, "NSE", Timeframe.WEEKLY)
        nyse = resolve_indicator_params(cfg, "NYSE", Timeframe.WEEKLY)
        assert nse.med_base == 10
        assert nyse.med_base == 12
        assert nse.atr_length == 21
        assert nyse.atr_length == 28


# ---------- Validation failures (load-time) ---------- #


class TestExchangeOverridesValidation:
    def test_unknown_exchange_rejected(self, tmp_path, db_pwd):
        """An exchange code in exchange_overrides that isn't in
        market_hours is rejected at config-load time, not silently
        ignored. This catches typos before they cause confusion."""
        yaml = _BASE_YAML + """
exchange_overrides:
  NSEX:
    "1W":
      ema:
        med:     { base: 8,  s1: 4, s2: 4 }
        long:    { base: 15, s1: 4, s2: 4, final: 3 }
        longest: { base: 28, s1: 7, s2: 5, final: 3 }
      atr: { length: 14, inner_mult: 0.2, mid_mult: 2.0, outer_mult: 4.0 }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError, match="unknown exchange"):
            load_config(p)

    def test_unsupported_timeframe_rejected(self, tmp_path, db_pwd):
        """Timeframes other than '1D' / '1W' are rejected. Phase 15
        explicitly supports only daily/weekly per design D3; intraday
        is deferred to a later phase."""
        yaml = _BASE_YAML + """
exchange_overrides:
  NSE:
    "1h":
      ema:
        med:     { base: 8,  s1: 4, s2: 4 }
        long:    { base: 15, s1: 4, s2: 4, final: 3 }
        longest: { base: 28, s1: 7, s2: 5, final: 3 }
      atr: { length: 14, inner_mult: 0.2, mid_mult: 2.0, outer_mult: 4.0 }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError, match="not supported by"):
            load_config(p)

    def test_extra_field_in_override_rejected(self, tmp_path, db_pwd):
        """ExchangeIndicatorOverride uses extra='forbid'; a typo'd
        sub-key (e.g. 'ena' instead of 'ema') surfaces immediately."""
        yaml = _BASE_YAML + """
exchange_overrides:
  NSE:
    "1W":
      ema:
        med:     { base: 8,  s1: 4, s2: 4 }
        long:    { base: 15, s1: 4, s2: 4, final: 3 }
        longest: { base: 28, s1: 7, s2: 5, final: 3 }
      atr: { length: 14, inner_mult: 0.2, mid_mult: 2.0, outer_mult: 4.0 }
      typo: 1
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError):
            load_config(p)

    def test_invalid_atr_multiplier_rejected(self, tmp_path, db_pwd):
        """ATRConfig requires multipliers > 0; an override with a
        zero multiplier is invalid and load fails."""
        yaml = _BASE_YAML + """
exchange_overrides:
  NSE:
    "1W":
      ema:
        med:     { base: 8,  s1: 4, s2: 4 }
        long:    { base: 15, s1: 4, s2: 4, final: 3 }
        longest: { base: 28, s1: 7, s2: 5, final: 3 }
      atr: { length: 14, inner_mult: 0.0, mid_mult: 2.0, outer_mult: 4.0 }
"""
        p = _write(tmp_path, yaml)
        with pytest.raises(ConfigError):
            load_config(p)


# ---------- Module-level constants ---------- #


class TestSupportedBacktestTimeframesConstant:
    def test_constant_matches_d3_design_decision(self):
        """Design D3: Phase 15 supports 1D and 1W only. Locked here
        so any change has to walk through this test."""
        assert frozenset({"1D", "1W"}) == SUPPORTED_BACKTEST_TIMEFRAMES
