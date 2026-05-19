"""Alembic environment for trip_ema.

Bridges Alembic's CLI to trip_ema's existing configuration loader so
migrations connect to the same DB the main service does. Reads the
DB URL from a config.yaml file rather than alembic.ini's
`sqlalchemy.url` — the password lives in an env var (per the existing
config pattern), and we don't want it duplicated in alembic.ini.

Phase 12 — Alembic is now the exclusive migration system:
  - On a fresh DB, `alembic upgrade head` runs 0001_initial_schema
    which creates the entire schema in one transaction. The legacy
    SQL files in migrations/legacy/ are preserved as historical
    reference only.
  - On a pre-Phase-12 DB (legacy migrator at version 010), the
    trip-ema startup invokes the alembic_bridge to stamp
    0001_initial_schema WITHOUT re-running its upgrade() function,
    then runs alembic upgrade head for any later revisions.

Usage:
  TRIP_EMA_CONFIG=config.yaml TRIP_EMA_DB_PASSWORD=... alembic upgrade head

  # Or explicitly:
  alembic -x config=/path/to/config.yaml upgrade head

  # For tests (bypass config.yaml loading, use a direct URL set on
  # the Alembic config object):
  alembic -x test_mode=1 upgrade head

The -x switch overrides the TRIP_EMA_CONFIG env var if both are set.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make `trip_ema` importable when env.py runs from the alembic CLI.
# alembic invokes us with cwd = repo root, so this is normally a no-op,
# but explicit is safer.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trip_ema.config.loader import load_config  # noqa: E402
from trip_ema.persistence.connection import build_db_url  # noqa: E402

# Alembic Config object — gives us access to alembic.ini values.
config = context.config

# Configure Python logging from alembic.ini's [logger_*] sections.
#
# CRITICAL: `disable_existing_loggers=False`. The fileConfig() default is
# True, which silently disables every logger NOT explicitly registered
# in alembic.ini's [loggers] section. Since our app sets up its own
# loggers earlier in the boot sequence (and pytest's caplog registers
# its handler against the root logger), the default behaviour wipes them
# out — making logger.info/.warning/.error calls in app code invisible
# AND making caplog.records empty in tests that exercise post-migration
# code paths. The False keeps Alembic-specific loggers configured while
# leaving everyone else's loggers intact.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

logger = logging.getLogger("alembic.env")


def _resolve_config_path() -> Path:
    """Find the trip_ema config.yaml. Priority:
      1. -x config=<path> on the alembic command line
      2. TRIP_EMA_CONFIG env var
      3. ./config.yaml in CWD
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if "config" in x_args:
        return Path(x_args["config"]).resolve()

    env_path = os.environ.get("TRIP_EMA_CONFIG")
    if env_path:
        return Path(env_path).resolve()

    fallback = Path.cwd() / "config.yaml"
    if not fallback.exists():
        raise RuntimeError(
            "Could not find trip_ema config.yaml. Set TRIP_EMA_CONFIG, "
            "use -x config=<path>, or place config.yaml in the CWD."
        )
    return fallback


def _is_test_mode() -> bool:
    """True if the alembic caller passed -x test_mode=1.

    In test mode, env.py skips config.yaml loading entirely and
    uses whatever sqlalchemy.url is already set on the Alembic
    config (the test fixture sets it directly). This avoids the
    test having to write a fake config.yaml just to satisfy the
    config loader.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    return x_args.get("test_mode") == "1"


def _build_db_url() -> str:
    """Build a SQLAlchemy URL from the trip_ema config."""
    cfg_path = _resolve_config_path()
    logger.info("Loading trip_ema config from %s", cfg_path)
    cfg = load_config(cfg_path)
    # Reuse build_db_url to keep password resolution logic
    # in one place; we never want to duplicate password handling.
    return build_db_url(cfg)


# target_metadata is None — we use raw SQL migrations rather than
# `autogenerate`-from-models. The trip_ema codebase doesn't have a
# single SQLAlchemy MetaData object covering every table (tables are
# defined via raw SQL in alembic_pkg/sql/). Setting this to None
# disables autogenerate but offline + online migrations work fine.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout rather
    than executing it against a live DB. Useful for review and for
    operators who want to inspect what `alembic upgrade head` would do.
    """
    if _is_test_mode():
        # Test fixtures set sqlalchemy.url directly on the config.
        url = config.get_main_option("sqlalchemy.url")
    else:
        url = _build_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # We use our own version table name to be explicit that this
        # is the trip_ema Alembic state (in case some other system
        # ever shares the DB).
        version_table="alembic_version",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    cfg_section = config.get_section(config.config_ini_section, {})
    if _is_test_mode():
        # Test fixtures set sqlalchemy.url via set_main_option, which
        # populates the section automatically.
        pass
    else:
        # Override the sqlalchemy.url that alembic.ini might have set
        # with the value from trip_ema's config.
        cfg_section["sqlalchemy.url"] = _build_db_url()

    connectable = engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="alembic_version",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
