"""Tests for the legacy-to-Alembic bridge.

Uses pytest-postgresql for a real Postgres instance because the
bridge's behavior depends on querying `schema_migrations` and
`alembic_version` — those queries can't be meaningfully mocked
without losing the test's value.

Pattern:
  - Each test starts with a fresh DB (no tables).
  - Manually create the legacy `schema_migrations` table and seed it
    to simulate "legacy migrator at version X".
  - Optionally create `alembic_version` and seed it to simulate
    "Alembic has run".
  - Call stamp_baseline_if_needed; assert the right thing happened.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text


pytest_plugins = ["pytest_postgresql"]


@pytest.fixture
def db_engine(postgresql):
    """SQLAlchemy engine wrapping the pytest-postgresql fixture."""
    info = postgresql.info
    url = (
        f"postgresql+psycopg2://{info.user}:@"
        f"{info.host}:{info.port}/{info.dbname}"
    )
    engine = create_engine(url)
    yield engine
    engine.dispose()


def _create_legacy_table(engine):
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE schema_migrations (
                version VARCHAR(16) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))


def _record_legacy_version(engine, version):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO schema_migrations (version) VALUES (:v)"),
            {"v": version},
        )


def _alembic_version_count(engine) -> int:
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT count(*) FROM alembic_version")
        )
        return result.scalar()


class TestLegacyHasReachedBaseline:
    """The _legacy_has_reached_baseline helper."""

    def test_returns_false_when_table_does_not_exist(self, db_engine):
        from trip_ema.persistence.alembic_bridge import (
            _legacy_has_reached_baseline,
        )
        assert _legacy_has_reached_baseline(db_engine) is False

    def test_returns_false_when_table_is_empty(self, db_engine):
        from trip_ema.persistence.alembic_bridge import (
            _legacy_has_reached_baseline,
        )
        _create_legacy_table(db_engine)
        assert _legacy_has_reached_baseline(db_engine) is False

    def test_returns_false_below_010(self, db_engine):
        from trip_ema.persistence.alembic_bridge import (
            _legacy_has_reached_baseline,
        )
        _create_legacy_table(db_engine)
        for v in ["001", "002", "005", "009"]:
            _record_legacy_version(db_engine, v)
        assert _legacy_has_reached_baseline(db_engine) is False

    def test_returns_true_at_010(self, db_engine):
        from trip_ema.persistence.alembic_bridge import (
            _legacy_has_reached_baseline,
        )
        _create_legacy_table(db_engine)
        for v in ["001", "002", "010"]:
            _record_legacy_version(db_engine, v)
        assert _legacy_has_reached_baseline(db_engine) is True

    def test_tolerates_non_numeric_versions(self, db_engine):
        """If someone stuffs a non-numeric version in the table by
        mistake, we should skip it rather than crash."""
        from trip_ema.persistence.alembic_bridge import (
            _legacy_has_reached_baseline,
        )
        _create_legacy_table(db_engine)
        _record_legacy_version(db_engine, "010")
        _record_legacy_version(db_engine, "weird")
        assert _legacy_has_reached_baseline(db_engine) is True


class TestAlembicHasAVersion:
    """The _alembic_has_a_version helper."""

    def test_returns_false_when_table_missing(self, db_engine):
        from trip_ema.persistence.alembic_bridge import (
            _alembic_has_a_version,
        )
        assert _alembic_has_a_version(db_engine) is False

    def test_returns_false_when_table_empty(self, db_engine):
        from trip_ema.persistence.alembic_bridge import (
            _alembic_has_a_version,
        )
        with db_engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            ))
        assert _alembic_has_a_version(db_engine) is False

    def test_returns_true_when_table_populated(self, db_engine):
        from trip_ema.persistence.alembic_bridge import (
            _alembic_has_a_version,
        )
        with db_engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            ))
            conn.execute(text(
                "INSERT INTO alembic_version (version_num) VALUES ('some_later_rev')"
            ))
        assert _alembic_has_a_version(db_engine) is True


class TestStampBaselineIfNeeded:
    """The public entry point."""

    def test_no_stamp_when_legacy_below_baseline(self, db_engine):
        """Legacy migrator at 005 — we shouldn't stamp anything."""
        from trip_ema.persistence.alembic_bridge import (
            stamp_baseline_if_needed,
        )
        _create_legacy_table(db_engine)
        _record_legacy_version(db_engine, "005")

        result = stamp_baseline_if_needed(db_engine)

        assert result is False
        # alembic_version should NOT have been created
        from sqlalchemy.exc import ProgrammingError
        with db_engine.connect() as conn, pytest.raises(ProgrammingError):
            conn.execute(text("SELECT 1 FROM alembic_version"))

    def test_no_stamp_when_alembic_already_populated(self, db_engine):
        """If alembic_version already has a row, don't touch it."""
        from trip_ema.persistence.alembic_bridge import (
            stamp_baseline_if_needed,
        )
        _create_legacy_table(db_engine)
        _record_legacy_version(db_engine, "010")
        with db_engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            ))
            conn.execute(text(
                "INSERT INTO alembic_version (version_num) VALUES ('some_later_rev')"
            ))

        result = stamp_baseline_if_needed(db_engine)

        assert result is False
        # The existing version must be untouched
        with db_engine.connect() as conn:
            existing = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
        assert existing == "some_later_rev"
