"""Config loader — read YAML file, validate into the Config schema.

Secrets (DB password) are never stored in the file. The file names an
*environment variable* that holds the secret. At load time we read both
the file and resolve the env var; an unset env var is a hard failure.

The environment is an ARGUMENT, not an ambient global. Every function here
takes `env: Mapping[str, str] | None`, defaulting to `os.environ` — which is
the production path and stays the production path. What it buys is that
config loading is a pure function of (yaml, env): tests pass a dict instead
of mutating the process environment, so they can't leak into each other and
can't fight each other under `pytest -n auto`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from .schema import Config

Env = Mapping[str, str]
"""A read-only environment. `os.environ` satisfies this; so does a plain dict."""


def _env(env: Env | None) -> Env:
    """The one place the ambient environment is reached for."""
    return os.environ if env is None else env


class ConfigError(Exception):
    """Raised when config is missing, malformed, or references unset env vars."""


def load_config(path: str | Path, *, env: Env | None = None) -> Config:
    """Read YAML from `path`, validate, and return a Config instance.

    Args:
        path: the YAML file.
        env: the environment to resolve secret env-var NAMES against.
            Defaults to `os.environ`. Pass a dict to load a config against a
            controlled environment (tests, and any future multi-tenant caller).

    Raises ConfigError on any failure — caller should let it propagate up
    to the process entry point.
    """
    environ = _env(env)
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    try:
        with p.open("r") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error in {p}: {e}") from e

    try:
        config = Config.model_validate(raw)
    except Exception as e:
        # Pydantic raises ValidationError; wrap so callers can catch a single type.
        raise ConfigError(f"Config validation failed for {p}: {e}") from e

    # config.yaml is the single source of truth for what a market is.
    # Seeding HERE, rather than asking every entry point to remember a
    # startup call, is the point: a missed entry point would silently
    # run on the hardcoded _EXCHANGE_SPECS instead of the operator's
    # config — which is exactly the duplication this removes, brought
    # back as a call-site obligation.
    #
    # market_session imports nothing from trip_ema, so there is no cycle.
    from ..domain.market_session import seed_specs_from_config

    seed_specs_from_config(config)

    # Sanity-check: the env var named for the DB password must be set.
    # We don't store the password on the Config object — we resolve it at
    # connect time. But fail fast at startup if it's missing.
    pwd_env = config.database.password_env
    if pwd_env not in environ:
        raise ConfigError(
            f"Database password env var '{pwd_env}' is not set"
        )

    # A5 fix: same check for Redis password when configured. RedisConfig
    # has the same `password_env` field as DatabaseConfig and the same
    # contract (lookup at connect-time). Without this fail-fast at boot,
    # a misconfigured Redis password would surface only deep inside the
    # NotifierWorker process after startup looked healthy.
    if config.redis is not None and config.redis.password_env:
        redis_pwd_env = config.redis.password_env
        if redis_pwd_env not in environ:
            raise ConfigError(
                f"Redis password env var '{redis_pwd_env}' is not set"
            )

    # Brevo secrets file: when `transports.brevo` is configured, the
    # secrets file must exist, be securely permissioned, and contain
    # a well-formed api_key. Same fail-fast contract as the DB and
    # Redis password checks above. This prevents the silent-
    # NullTransport bug class: an operator who configures Brevo in
    # config.yaml but forgets to deploy the secrets file should see
    # a hard startup failure, not silent loss of email delivery.
    if config.transports.brevo is not None:
        from .brevo_secrets import BrevoSecretsError, load_brevo_api_key
        try:
            load_brevo_api_key(config.transports.brevo.secrets_file)
        except BrevoSecretsError as e:
            raise ConfigError(str(e)) from e

    return config


def resolve_db_password(config: Config, *, env: Env | None = None) -> str:
    """Resolve the DB password from the env var named in the config.

    Call this at connection time. Will KeyError if the var has been unset
    since load_config ran — that's a defensive check, normally load_config
    catches the missing-env-var case at startup.

    `env` defaults to os.environ; pass a dict to resolve against a controlled
    environment.
    """
    return _env(env)[config.database.password_env]


def resolve_redis_password(
    config: Config, *, env: Env | None = None,
) -> str | None:
    """Resolve the Redis password (if configured) from its env var.

    Returns None if no Redis config exists or if Redis config has no
    password_env field (open dev Redis case). Raises KeyError if the
    var has been unset since load_config ran (same defensive contract
    as resolve_db_password).
    """
    if config.redis is None or not config.redis.password_env:
        return None
    return _env(env)[config.redis.password_env]
