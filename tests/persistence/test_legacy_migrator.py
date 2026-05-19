"""Regression tests for the legacy SQL migrator.

Phase 12 made Alembic the exclusive migration system. The legacy
migrator (`trip_ema.persistence.migrator.apply_pending`) is preserved
because:

  1. Its tests in this file verify the SQL files in migrations/legacy/
     still produce a valid schema. This catches accidental damage to
     the archived files (e.g., someone edits 003_subscribers.sql).
  2. The version-check probe (_check_postgres_version) is still
     exercised here as a sanity check.

These tests use a dedicated fixture that runs the legacy migrator
against migrations/legacy/ — they do NOT use the standard db_engine
fixture (which now uses Alembic). The two paths should converge on
the same schema; if they ever diverge, the legacy archive and the
squashed Alembic revision have drifted apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_postgresql import factories
from sqlalchemy import Engine, create_engine, text


LEGACY_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "legacy"


# Phase 14.5: parity test needs TWO independent databases — one migrated
# by the legacy SQL runner, one by Alembic — to compare their schemas.
# The default `postgresql` fixture is function-scoped so each test gets
# a fresh DB, but it only gives ONE DB per test. Requesting both
# `legacy_db_engine` and `db_engine` in the same test resolves the
# `postgresql` dependency once and SHARES the DB between the two
# fixtures, which means whichever migration system runs second tries to
# CREATE objects the first one already created. The result was a
# DuplicateObject error inside Alembic revision 0001.
#
# Solution: register a second factory bound to a different database
# name on the same Postgres process. `parity_alembic_postgresql` is
# created fresh per test (function-scope, default), and points at a
# distinct database from the legacy_db_engine's `postgresql`.
parity_alembic_postgresql = factories.postgresql(
    "postgresql_proc", dbname="parity_alembic",
)


@pytest.fixture
def legacy_db_engine(postgresql) -> Engine:
    """A SQLAlchemy engine pointed at a fresh DB, migrated using the
    LEGACY SQL runner against migrations/legacy/. Used only by tests
    in this file."""
    from trip_ema.persistence.migrator import apply_pending

    conn_info = postgresql.info
    url = (
        f"postgresql+psycopg2://{conn_info.user}@"
        f"{conn_info.host}:{conn_info.port}/{conn_info.dbname}"
    )
    engine = create_engine(url, future=True)
    apply_pending(engine, LEGACY_MIGRATIONS_DIR)
    yield engine
    engine.dispose()


# ============================================================
# Legacy migrator behavior
# ============================================================

def test_legacy_migrations_apply_cleanly(legacy_db_engine):
    """The archived SQL files in migrations/legacy/ still produce a
    populated schema_migrations table."""
    with legacy_db_engine.connect() as conn:
        rows = list(conn.execute(text(
            "SELECT version FROM schema_migrations ORDER BY version"
        )))
        versions = [r[0] for r in rows]
    expected = sorted([
        p.name.split("_")[0]
        for p in LEGACY_MIGRATIONS_DIR.iterdir()
        if p.suffix == ".sql"
    ])
    assert versions == expected


def test_legacy_idempotent(legacy_db_engine):
    """A second apply_pending call is a no-op."""
    from trip_ema.persistence.migrator import apply_pending

    newly_applied = apply_pending(legacy_db_engine, LEGACY_MIGRATIONS_DIR)
    assert newly_applied == []


def test_legacy_produces_expected_tables(legacy_db_engine):
    """Sanity-check the legacy schema matches what Phase 1-11 docs
    describe."""
    expected_tables = {
        "tickers", "ohlcv_bars",
        "signal_events",
        "subscribers", "subscriptions",
        "notification_log",
        "schema_migrations",
    }
    with legacy_db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        ))
        present = {r[0] for r in rows}
    assert expected_tables.issubset(present), (
        f"Missing: {expected_tables - present}"
    )
    assert "ticker_signal_state" not in present, (
        "ticker_signal_state should have been dropped by migration 005"
    )


def test_legacy_partitions_seeded(legacy_db_engine):
    """Migration 004 seeded several monthly notification_log partitions."""
    with legacy_db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT relname FROM pg_class WHERE relname LIKE 'notification_log_%'"
        ))
        partitions = {r[0] for r in rows}
    assert len(partitions) >= 4


# ============================================================
# C1: Postgres version probe
# ============================================================

def test_version_check_passes_on_supported_version(legacy_db_engine):
    """C1: _check_postgres_version is a no-op against Postgres 15+."""
    from trip_ema.persistence.migrator import _check_postgres_version
    _check_postgres_version(legacy_db_engine)


def test_version_check_raises_with_high_minimum(legacy_db_engine):
    from trip_ema.persistence.migrator import _check_postgres_version
    with pytest.raises(RuntimeError, match=r"PostgreSQL >= 99\.x"):
        _check_postgres_version(legacy_db_engine, min_major=99)


def test_version_check_message_mentions_migration_003(legacy_db_engine):
    """Error message references the specific feature (NULLS NOT
    DISTINCT) so ops know which migration was the trigger."""
    from trip_ema.persistence.migrator import _check_postgres_version
    try:
        _check_postgres_version(legacy_db_engine, min_major=99)
    except RuntimeError as e:
        assert "NULLS NOT DISTINCT" in str(e)
    else:
        pytest.fail("expected RuntimeError")


# ============================================================
# Alembic ↔ Legacy schema parity
# ============================================================

@pytest.fixture
def parity_alembic_db_engine(parity_alembic_postgresql) -> Engine:
    """A SQLAlchemy engine pointed at a SEPARATE database (distinct from
    the one `legacy_db_engine` uses), migrated by Alembic. Created
    specifically for the parity test below — see the comment on
    `parity_alembic_postgresql` for why a dedicated fixture is needed.

    We re-use the conftest's `_apply_alembic_upgrade` helper rather
    than calling Alembic directly here, so any future change to the
    Alembic invocation path stays in one place."""
    from tests.persistence.conftest import _apply_alembic_upgrade

    conn_info = parity_alembic_postgresql.info
    url = (
        f"postgresql+psycopg2://{conn_info.user}@"
        f"{conn_info.host}:{conn_info.port}/{conn_info.dbname}"
    )
    _apply_alembic_upgrade(url)
    engine = create_engine(url, future=True)
    yield engine
    engine.dispose()


def test_alembic_and_legacy_produce_same_tables(
    legacy_db_engine, parity_alembic_db_engine,
):
    """The squashed Alembic revision must produce the same set of
    tables as the legacy SQL files. If this ever fails, either:
      - Someone changed migrations/legacy/*.sql (don't), or
      - Someone changed alembic_pkg/sql/0001_initial_schema.sql in
        a way that drifted from the legacy schema.

    Uses two DISTINCT databases (`postgresql` and
    `parity_alembic_postgresql`) so the two migration systems can run
    in isolation. The default `db_engine` fixture shares the same DB
    with `legacy_db_engine` when both are requested, which used to
    cause the second migrator to step on the first's schema.
    """
    def _tables(engine):
        with engine.connect() as conn:
            return {
                r[0] for r in conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ))
            }

    legacy_tables = _tables(legacy_db_engine)
    alembic_tables = _tables(parity_alembic_db_engine)

    # Ignore migration-bookkeeping tables on BOTH sides. The legacy
    # migrator's bookkeeping table is `schema_migrations`; Alembic's
    # is `alembic_version`. As of Phase 12, the bridge also creates
    # `schema_migrations` on the Alembic side (so legacy DBs that
    # later get switched to Alembic still have the legacy bookkeeping
    # preserved); from the schema-parity standpoint, neither counts.
    _IGNORED = {"schema_migrations", "alembic_version"}
    legacy_core = legacy_tables - _IGNORED
    alembic_core = alembic_tables - _IGNORED

    assert legacy_core == alembic_core, (
        f"Schema drift: only in legacy={legacy_core - alembic_core}, "
        f"only in alembic={alembic_core - legacy_core}"
    )
