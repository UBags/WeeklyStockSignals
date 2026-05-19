"""Tests for the migration runner."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text


MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def test_migrations_apply_cleanly_on_empty_db(db_engine):
    """The db_engine fixture already runs apply_pending; here we verify
    that the schema_migrations table is populated correctly."""
    with db_engine.connect() as conn:
        rows = list(conn.execute(text(
            "SELECT version FROM schema_migrations ORDER BY version"
        )))
        versions = [r[0] for r in rows]
    # We expect every version in /migrations to be recorded.
    expected = sorted([
        p.name.split("_")[0]
        for p in MIGRATIONS_DIR.iterdir()
        if p.suffix == ".sql"
    ])
    assert versions == expected


def test_applying_migrations_twice_is_idempotent(db_engine):
    """A second `alembic upgrade head` after a clean migration must be
    a no-op — important property since the bridge runs it on every
    startup.

    Phase 12 retired the legacy `apply_pending` SQL-file migrator in
    favor of Alembic. The legacy migrator is gone; this test now
    exercises the Alembic equivalent. The behavior we care about
    (a redundant upgrade is harmless) is unchanged.

    Note: the `db_engine` fixture already ran one Alembic upgrade
    during setup. This call is the SECOND, and is what must be
    a no-op."""
    from trip_ema.persistence.alembic_bridge import (
        _alembic_has_a_version, run_alembic_upgrade_head,
    )

    # Sanity: fixture's upgrade landed us at head.
    assert _alembic_has_a_version(db_engine) is True

    # Re-run — must not raise, must not change schema. We can't easily
    # introspect "0 revisions applied" from Alembic's CLI surface
    # without scraping stdout, so verifying success + still-at-head
    # is the strongest check we can make from here.
    run_alembic_upgrade_head()
    assert _alembic_has_a_version(db_engine) is True


def test_expected_tables_exist(db_engine):
    """Sanity-check that the expected tables came out of the migrations.
    Note: ticker_signal_state was dropped in migration 005."""
    expected_tables = {
        "tickers", "ohlcv_bars",
        "signal_events",
        "subscribers", "subscriptions",
        "notification_log",
        "schema_migrations",
    }
    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        ))
        present = {r[0] for r in rows}
    assert expected_tables.issubset(present), (
        f"Missing: {expected_tables - present}"
    )
    # And ticker_signal_state must be ABSENT
    assert "ticker_signal_state" not in present, (
        "ticker_signal_state should have been dropped by migration 005"
    )


def test_notification_log_is_partitioned(db_engine):
    """notification_log must be a partitioned table (not a regular one)."""
    with db_engine.connect() as conn:
        result = conn.execute(text(
            "SELECT relkind FROM pg_class WHERE relname = 'notification_log'"
        )).scalar_one()
    # 'p' = partitioned table
    assert result == "p"


def test_partitions_created_for_current_window(db_engine):
    """Migration 004 should have seeded several monthly partitions."""
    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT relname FROM pg_class WHERE relname LIKE 'notification_log_%'"
        ))
        partitions = {r[0] for r in rows}
    # We seeded prev, current, next, +2 — at least 4 partitions.
    assert len(partitions) >= 4


# ============================================================
# C1: Postgres version probe
# ============================================================

def test_version_check_passes_on_supported_version(db_engine):
    """C1: _check_postgres_version() should be a no-op against Postgres 15+
    (which the test runner uses). This implicitly verifies the probe
    integrates cleanly with apply_pending."""
    from trip_ema.persistence.migrator import _check_postgres_version
    # No exception → check passed.
    _check_postgres_version(db_engine)


def test_version_check_raises_with_high_minimum(db_engine):
    """If we artificially bump min_major above the running version,
    the probe should raise RuntimeError with a clear message."""
    from trip_ema.persistence.migrator import _check_postgres_version
    with pytest.raises(RuntimeError, match=r"PostgreSQL >= 99\.x"):
        _check_postgres_version(db_engine, min_major=99)


def test_version_check_message_mentions_migration_003(db_engine):
    """The error message should reference the specific feature
    (NULLS NOT DISTINCT) so ops know which migration was the trigger."""
    from trip_ema.persistence.migrator import _check_postgres_version
    try:
        _check_postgres_version(db_engine, min_major=99)
    except RuntimeError as e:
        assert "NULLS NOT DISTINCT" in str(e)
    else:
        pytest.fail("expected RuntimeError")
