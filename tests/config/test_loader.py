"""Tests for the YAML config loader.

The loader takes `env` (a Mapping) rather than reaching for os.environ, so
these tests pass a dict. Exactly one test — test_default_env_is_os_environ —
touches the real process environment, to prove the default still is it.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from trip_ema.config import Config, ConfigError, load_config, resolve_db_password
from trip_ema.domain import IndicatorParams, Timeframe


ENV = {"TEST_DB_PWD": "x"}
"""Default env for tests that only need the DB password to be *present*."""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return p


VALID_YAML = """
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
"""


def test_load_valid_config(tmp_path):
    p = _write(tmp_path, VALID_YAML)
    config = load_config(p, env={"TEST_DB_PWD": "secret"})
    assert isinstance(config, Config)
    assert config.database.user == "app"
    assert config.ema.med.base == 8
    assert config.ema.med.final is None
    assert config.scheduling.default_timeframe == Timeframe.WEEKLY


def test_password_env_unset_raises(tmp_path):
    p = _write(tmp_path, VALID_YAML)
    with pytest.raises(ConfigError, match="password env var"):
        load_config(p, env={})


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "doesnt_exist.yaml")


def test_malformed_yaml_raises(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("database: { unclosed brace")
    with pytest.raises(ConfigError, match="YAML parse error"):
        load_config(p)


def test_extra_fields_rejected(tmp_path):
    """Forbidden-extra config catches typos before they cause silent failures."""
    body = VALID_YAML + "\nunknown_section:\n  foo: bar\n"
    p = _write(tmp_path, body)
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(p, env=ENV)


def test_invalid_ema_length_rejected(tmp_path):
    bad = VALID_YAML.replace("base: 8", "base: 1")  # EMA base must be >= 2
    p = _write(tmp_path, bad)
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(p, env=ENV)


def test_invalid_weekday_rejected(tmp_path):
    bad = VALID_YAML.replace("weekdays: [1, 2, 3, 4, 5]", "weekdays: [0, 9]")
    p = _write(tmp_path, bad)
    with pytest.raises(ConfigError, match="validation failed"):
        load_config(p, env=ENV)


def test_to_indicator_params_flattens_correctly(tmp_path):
    p = _write(tmp_path, VALID_YAML)
    config = load_config(p, env=ENV)
    params = config.to_indicator_params()
    assert isinstance(params, IndicatorParams)
    assert params.med_base == 8
    assert params.med_final is None
    assert params.long_final == 3
    assert params.longest_base == 28
    assert params.atr_length == 14
    assert params.outer_mult == 4.0


def test_resolve_password(tmp_path):
    env = {"TEST_DB_PWD": "hunter2"}
    p = _write(tmp_path, VALID_YAML)
    config = load_config(p, env=env)
    assert resolve_db_password(config, env=env) == "hunter2"


def test_default_env_is_os_environ(tmp_path, monkeypatch):
    """The env seam DEFAULTS to the real process environment — that is the
    production path, and this is the one test in this module that proves it.
    Everything else passes an explicit dict, which is why nothing else here
    touches os.environ."""
    monkeypatch.setenv("TEST_DB_PWD", "from-os-environ")
    p = _write(tmp_path, VALID_YAML)
    config = load_config(p)
    assert resolve_db_password(config) == "from-os-environ"


def test_url_without_password_hides_secret(tmp_path):
    p = _write(tmp_path, VALID_YAML)
    config = load_config(p, env={"TEST_DB_PWD": "secret"})
    url = config.database.url_without_password()
    assert "secret" not in url
    assert "***" in url
    assert "app" in url


def test_example_yaml_is_valid():
    """The bundled example.yaml must always pass validation."""
    example_path = Path(__file__).resolve().parents[2] / "trip_ema" / "config" / "example.yaml"
    assert example_path.exists(), f"example.yaml missing at {example_path}"
    config = load_config(example_path, env={"TRIP_EMA_DB_PASSWORD": "anything"})
    assert config is not None
    assert len(config.market_hours) >= 1


# ============================================================
# A5: Redis password env var validation
# ============================================================

VALID_YAML_WITH_REDIS = VALID_YAML + """
redis:
  host: localhost
  port: 6379
  db: 0
  password_env: TEST_REDIS_PWD
"""

VALID_YAML_REDIS_NO_PASSWORD = VALID_YAML + """
redis:
  host: localhost
  port: 6379
  db: 0
"""


def test_redis_password_env_unset_raises(tmp_path):
    """A5: When redis.password_env is configured but the env var is
    missing, load_config must fail at boot with a clear error."""
    p = _write(tmp_path, VALID_YAML_WITH_REDIS)
    with pytest.raises(ConfigError, match="Redis password env var"):
        load_config(p, env={"TEST_DB_PWD": "hunter2"})


def test_redis_without_password_env_succeeds(tmp_path):
    """When redis is configured but password_env is unset (open dev
    Redis), load_config should succeed."""
    p = _write(tmp_path, VALID_YAML_REDIS_NO_PASSWORD)
    config = load_config(p, env={"TEST_DB_PWD": "hunter2"})
    assert config.redis is not None
    assert config.redis.password_env is None


def test_resolve_redis_password_returns_none_without_redis(tmp_path):
    """resolve_redis_password handles missing redis config cleanly."""
    from trip_ema.config.loader import resolve_redis_password
    env = {"TEST_DB_PWD": "hunter2"}
    p = _write(tmp_path, VALID_YAML)   # no redis section
    config = load_config(p, env=env)
    assert resolve_redis_password(config, env=env) is None


def test_resolve_redis_password_returns_value_when_set(tmp_path):
    from trip_ema.config.loader import resolve_redis_password
    env = {"TEST_DB_PWD": "hunter2", "TEST_REDIS_PWD": "rediss3cret"}
    p = _write(tmp_path, VALID_YAML_WITH_REDIS)
    config = load_config(p, env=env)
    assert resolve_redis_password(config, env=env) == "rediss3cret"


# ============================================================
# DB pool config (Phase 10)
# ============================================================


def test_db_pool_defaults_are_sensible(tmp_path):
    """Without explicit pool fields in YAML, defaults must match the
    documented values."""
    p = _write(tmp_path, VALID_YAML)
    config = load_config(p, env=ENV)
    assert config.database.pool_size == 5
    assert config.database.pool_max_overflow == 10
    assert config.database.pool_timeout_seconds == 30
    assert config.database.pool_recycle_seconds == 3600


def test_db_pool_overrides_from_yaml(tmp_path):
    """Custom values in YAML must override the defaults."""
    yaml = VALID_YAML.replace(
        "  password_env: TEST_DB_PWD",
        "  password_env: TEST_DB_PWD\n"
        "  pool_size: 20\n"
        "  pool_max_overflow: 40\n"
        "  pool_timeout_seconds: 5\n"
        "  pool_recycle_seconds: 1800",
    )
    p = _write(tmp_path, yaml)
    config = load_config(p, env=ENV)
    assert config.database.pool_size == 20
    assert config.database.pool_max_overflow == 40
    assert config.database.pool_timeout_seconds == 5
    assert config.database.pool_recycle_seconds == 1800


def test_db_pool_size_below_one_rejected(tmp_path):
    """pool_size must be >= 1; smaller values would cause connection
    starvation under any concurrent load."""
    yaml = VALID_YAML.replace(
        "  password_env: TEST_DB_PWD",
        "  password_env: TEST_DB_PWD\n  pool_size: 0",
    )
    p = _write(tmp_path, yaml)
    with pytest.raises(ConfigError):
        load_config(p, env=ENV)


def test_db_pool_recycle_negative_one_disables(tmp_path):
    """The documented sentinel '-1' for 'never recycle' must be accepted."""
    yaml = VALID_YAML.replace(
        "  password_env: TEST_DB_PWD",
        "  password_env: TEST_DB_PWD\n  pool_recycle_seconds: -1",
    )
    p = _write(tmp_path, yaml)
    config = load_config(p, env=ENV)
    assert config.database.pool_recycle_seconds == -1

