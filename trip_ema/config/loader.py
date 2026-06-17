"""Config loader — read YAML file, validate into the Config schema.

Secrets (DB password) are never stored in the file. The file names an
*environment variable* that holds the secret. At load time we read both
the file and resolve the env var; an unset env var is a hard failure.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .schema import Config


class ConfigError(Exception):
    """Raised when config is missing, malformed, or references unset env vars."""


def load_config(path: str | Path) -> Config:
    """Read YAML from `path`, validate, and return a Config instance.

    Raises ConfigError on any failure — caller should let it propagate up
    to the process entry point.
    """
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

    # Sanity-check: the env var named for the DB password must be set.
    # We don't store the password on the Config object — we resolve it at
    # connect time. But fail fast at startup if it's missing.
    pwd_env = config.database.password_env
    if pwd_env not in os.environ:
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
        if redis_pwd_env not in os.environ:
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


def resolve_db_password(config: Config) -> str:
    """Resolve the DB password from the env var named in the config.

    Call this at connection time. Will KeyError if the var has been unset
    since load_config ran — that's a defensive check, normally load_config
    catches the missing-env-var case at startup.
    """
    return os.environ[config.database.password_env]


def resolve_redis_password(config: Config) -> str | None:
    """Resolve the Redis password (if configured) from its env var.

    Returns None if no Redis config exists or if Redis config has no
    password_env field (open dev Redis case). Raises KeyError if the
    var has been unset since load_config ran (same defensive contract
    as resolve_db_password).
    """
    if config.redis is None or not config.redis.password_env:
        return None
    return os.environ[config.redis.password_env]
