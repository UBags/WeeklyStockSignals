"""Tiny migration runner.

Discovers `NNN_*.sql` files in the migrations directory, applies each
one inside a transaction, and records its version in `schema_migrations`.
Skips versions already applied.

This is intentionally minimal — no rollback, no version skipping, no
schema diffing. For a small system whose schema evolves slowly, that's
enough. If we ever need more, we switch to Alembic; until then, the
simplicity is a feature.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

logger = logging.getLogger(__name__)


_MIGRATION_FILE_RE = re.compile(r"^(\d+)_.+\.sql$")


def _discover_migrations(migrations_dir: Path) -> list[tuple[str, Path]]:
    """Return (version, path) tuples sorted by version.

    Version is the numeric prefix as a string ('001', '002', ...). We keep
    it as a string to match how it's stored in `schema_migrations.version`.
    """
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {migrations_dir}")

    out: list[tuple[str, Path]] = []
    for entry in migrations_dir.iterdir():
        if not entry.is_file():
            continue
        m = _MIGRATION_FILE_RE.match(entry.name)
        if not m:
            continue
        version = m.group(1)
        out.append((version, entry))

    out.sort(key=lambda x: int(x[0]))
    return out


def _applied_versions(engine: Engine) -> set[str]:
    """Read schema_migrations. If the table doesn't exist yet (first run),
    return an empty set."""
    with engine.connect() as conn:
        try:
            result = conn.execute(text("SELECT version FROM schema_migrations"))
            return {row[0] for row in result}
        except ProgrammingError:
            # Table doesn't exist yet — first migration will create it.
            return set()


def _check_postgres_version(engine: Engine, min_major: int = 15) -> None:
    """C1: Fail fast with a clear message if Postgres is too old.

    Migration 003 uses `UNIQUE NULLS NOT DISTINCT`, which is a Postgres
    15+ feature. Without this probe, the migrator would crash with a
    cryptic "syntax error at or near NOT" on Postgres 14 or earlier.
    """
    with engine.connect() as conn:
        version_str = conn.execute(text("SHOW server_version")).scalar()
    if version_str is None:
        logger.warning(
            "Could not read Postgres version (SHOW server_version "
            "returned None); skipping version check",
        )
        return
    # `version_str` looks like "16.2 (Ubuntu 16.2-1.pgdg22.04+1)" or "14.10".
    # Extract the major-version integer.
    try:
        major = int(version_str.split(".")[0].split()[0])
    except (ValueError, AttributeError, IndexError):
        # If we can't parse it, log a warning and proceed — better to
        # try the migration and get a real error than to refuse to start
        # because of a version-string format we don't recognize.
        logger.warning(
            "Could not parse Postgres version '%s'; skipping version check",
            version_str,
        )
        return
    if major < min_major:
        raise RuntimeError(
            f"trip_ema requires PostgreSQL >= {min_major}.x; "
            f"found {version_str}. Migration 003 uses "
            f"`UNIQUE NULLS NOT DISTINCT` which is unavailable on older "
            f"versions."
        )


def apply_pending(engine: Engine, migrations_dir: Path) -> list[str]:
    """Apply every migration in `migrations_dir` that hasn't been applied yet.

    Returns the list of versions newly applied (empty list if nothing was
    pending). Each migration runs in its own transaction; a failure aborts
    that migration but doesn't roll back earlier ones.

    Fails fast if Postgres is older than 15 (the system uses
    `UNIQUE NULLS NOT DISTINCT` which requires Postgres 15+).
    """
    _check_postgres_version(engine)

    applied = _applied_versions(engine)
    pending = [
        (v, p) for v, p in _discover_migrations(migrations_dir)
        if v not in applied
    ]

    newly_applied: list[str] = []
    for version, path in pending:
        logger.info("Applying migration %s: %s", version, path.name)
        sql = path.read_text()
        # Use the raw DBAPI cursor (not SQLAlchemy's exec_driver_sql) so the
        # driver doesn't try to parse % as a parameter placeholder. Migration
        # files contain literal SQL (including `%` in modulus expressions) and
        # must be executed verbatim.
        with engine.begin() as conn:
            raw_conn = conn.connection           # the underlying DBAPI connection
            # psycopg2 cursors ARE context managers; the DBAPI
            # Protocol stubs don't declare __enter__/__exit__.
            with raw_conn.cursor() as cur:  # type: ignore[attr-defined]
                cur.execute(sql)
        newly_applied.append(version)
        logger.info("Migration %s applied successfully", version)

    return newly_applied
