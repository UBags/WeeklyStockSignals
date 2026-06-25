"""Bridge between legacy `schema_migrations` and Alembic.

Phase 12 made Alembic the exclusive migration system. The legacy SQL
runner (`trip_ema.persistence.migrator`) is preserved for its history
but no longer drives startup migrations. This module handles the
boundary case for databases that already ran the legacy migrator
through version 010 and now need to be told "you're at Alembic's
squashed initial revision."

Three possible states a database can be in when `trip-ema` starts:

  (A) Fresh DB: neither `schema_migrations` nor `alembic_version`
      exists. Action: `alembic upgrade head` creates everything.
      The bridge is a no-op.

  (B) Pre-Phase-12 DB: `schema_migrations` is populated through
      version 010, `alembic_version` doesn't exist. Action: bridge
      stamps `0001_initial_schema` (the squashed equivalent of
      legacy 001-010) WITHOUT re-running the DDL, then
      `alembic upgrade head` applies any later revisions.

  (C) Post-Phase-12 DB: `alembic_version` is populated. The bridge
      is a no-op. `alembic upgrade head` applies new revisions if
      any are pending.

Public API:
  stamp_baseline_if_needed(engine) — call once at startup BEFORE
    running `alembic upgrade head`. Idempotent. Returns True if a
    stamp was performed, False otherwise.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

logger = logging.getLogger(__name__)


# The baseline revision ID — must match the `revision = "..."`
# string in alembic_pkg/versions/0001_initial_schema.py.
#
# Phase 12: this is now the squashed initial revision (formerly
# 011_baseline as a no-op marker). Stamping it on a pre-Phase-12
# database tells Alembic "the schema state matches what
# 0001_initial_schema would have produced," which is true because
# the legacy migrator's 001-010 produced the same schema.
_BASELINE_REVISION = "0001_initial_schema"

# The legacy migration version that marks "post-Phase-11" — once this
# version exists in schema_migrations, we know it's safe to stamp the
# Alembic baseline.
_PHASE_11_LEGACY_VERSION = "010"


def _legacy_has_reached_baseline(engine: Engine) -> bool:
    """True iff schema_migrations contains version '010' (or higher
    numeric version). False if the table doesn't exist or if version
    010 isn't recorded yet."""
    with engine.connect() as conn:
        try:
            result = conn.execute(
                text("SELECT version FROM schema_migrations")
            )
            versions = {row[0] for row in result}
        except ProgrammingError:
            # Table doesn't exist — legacy migrator never ran.
            return False
    # Either '010' is exactly there, or some higher numeric version
    # (defensive — accommodates a future where someone adds 011 to
    # the legacy directory by mistake).
    for v in versions:
        try:
            if int(v) >= int(_PHASE_11_LEGACY_VERSION):
                return True
        except ValueError:
            continue
    return False


def _alembic_version_table_exists(engine: Engine) -> bool:
    with engine.connect() as conn:
        try:
            conn.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))
            return True
        except ProgrammingError:
            return False


def _alembic_has_a_version(engine: Engine) -> bool:
    """True iff alembic_version exists AND has at least one row."""
    if not _alembic_version_table_exists(engine):
        return False
    with engine.connect() as conn:
        result = conn.execute(text("SELECT version_num FROM alembic_version"))
        return result.first() is not None


def stamp_baseline_if_needed(
    engine: Engine,
    *,
    config_path: Path | None = None,
) -> bool:
    """If the legacy migrator has reached the post-Phase-11 state and
    Alembic hasn't been initialized yet, stamp the baseline revision.

    Args:
        engine: SQLAlchemy engine pointing at the DB to stamp.
        config_path: Path to the trip_ema config.yaml. Forwarded to
            Alembic's env.py via the TRIP_EMA_CONFIG mechanism. If
            None, env.py falls back to ./config.yaml in CWD.

    Returns:
        True if the baseline was stamped on this call; False if no
        stamp was needed (either Alembic is already initialized, or
        the legacy migrator hasn't reached the baseline yet).

    Raises:
        Anything Alembic raises if the stamp itself fails. The most
        likely cause is a missing alembic.ini in CWD.
    """
    if _alembic_has_a_version(engine):
        logger.debug(
            "Alembic version table already populated; skipping baseline stamp"
        )
        return False

    if not _legacy_has_reached_baseline(engine):
        logger.debug(
            "Legacy migrator has not reached %s yet; skipping baseline stamp",
            _PHASE_11_LEGACY_VERSION,
        )
        return False

    logger.info(
        "Stamping Alembic baseline revision %s (legacy migrator at >= %s)",
        _BASELINE_REVISION, _PHASE_11_LEGACY_VERSION,
    )

    # Defer the alembic import so the rest of trip_ema doesn't pay
    # the import cost when nobody calls this function. Also lets the
    # codebase boot on systems without alembic installed (the only
    # consequence is that no auto-stamping happens; ops can run
    # `alembic stamp 0001_initial_schema` by hand).
    try:
        from alembic import command
        from alembic.config import Config as AlembicConfig
    except ImportError:
        logger.warning(
            "Alembic is not installed; skipping baseline stamp. "
            "Install with `pip install trip-ema[migrations]` and run "
            "`alembic stamp %s` manually.", _BASELINE_REVISION,
        )
        return False

    alembic_cfg = AlembicConfig("alembic.ini")
    if config_path is not None:
        # env.py reads -x config=... before falling back to env var.
        # Stub-deficiency: Config.cmd_opts is typed Namespace | None,
        # but the documented -x injection uses an ad-hoc obj exposing .x
        alembic_cfg.cmd_opts = type("X", (), {"x": [f"config={config_path}"]})()  # pyright: ignore[reportAttributeAccessIssue]

    command.stamp(alembic_cfg, _BASELINE_REVISION)
    logger.info("Alembic baseline stamped.")
    return True


def run_alembic_upgrade_head(
    config_path: Path | None = None,
) -> None:
    """Apply any pending Alembic revisions up to head. Wraps
    `alembic upgrade head` so app.py has a single call site.

    On a fresh database, this creates the entire schema by running
    `0001_initial_schema.upgrade()`. On a database that's already at
    head (or that the bridge just stamped), this is a no-op.

    Args:
        config_path: Path to the trip_ema config.yaml. Forwarded to
            Alembic's env.py via -x config=... so DB credentials are
            read from the same place as the rest of trip_ema.

    Raises:
        Anything Alembic raises. The likely failure mode is a missing
        alembic.ini in CWD; the trip-ema entry point sets CWD to the
        repo root, so this should not happen in production.
    """
    try:
        from alembic import command
        from alembic.config import Config as AlembicConfig
    except ImportError:
        # If alembic isn't installed, we can't run migrations. On a
        # fresh DB this is fatal (no schema!); on a pre-Phase-12 DB
        # it just means we can't apply any post-squash migrations.
        # Either way, the caller deserves to know.
        raise RuntimeError(
            "Alembic is required to apply migrations. Install with "
            "`pip install trip-ema[migrations]`."
        ) from None

    alembic_cfg = AlembicConfig("alembic.ini")
    if config_path is not None:
        # Same stub-deficiency as the stamp path above.
        alembic_cfg.cmd_opts = type("X", (), {"x": [f"config={config_path}"]})()  # pyright: ignore[reportAttributeAccessIssue]

    logger.info("Running alembic upgrade head")
    command.upgrade(alembic_cfg, "head")
    logger.info("Alembic upgrade head complete")
