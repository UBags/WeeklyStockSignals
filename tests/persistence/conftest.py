"""Persistence test fixtures.

We test against a real Postgres instance (not sqlite) because the schema
uses Postgres-specific features (JSONB, partitioning, NULLS NOT DISTINCT).
Sqlite can't run the migrations, so any sqlite-based test would be
exercising a different schema than production.

The fixture uses `pytest-postgresql` to spin up an ephemeral Postgres for
the test session. Tests get a clean schema by running Alembic against
a fresh database each time the `db_engine` fixture is requested.

Phase 12 update: this conftest used to call `apply_pending` from the
legacy migrator. It now runs `alembic upgrade head` against each fresh
database via a direct Alembic API call (not subprocess) so the test
inherits the same migration flow production uses.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest_postgresql import factories
from sqlalchemy import Engine, create_engine

# Help pytest-postgresql find the Postgres binaries when they're not on PATH.
_PG_BIN_DEFAULT = "/usr/lib/postgresql/16/bin"
_pg_bin = os.environ.get("TRIP_EMA_PG_BIN_PATH", _PG_BIN_DEFAULT)


# Session-scoped Postgres process — started once per pytest run.
postgresql_proc = factories.postgresql_proc(
    executable=os.path.join(_pg_bin, "pg_ctl"),
    port=None,
)

# Function-scoped database — fresh DB per test.
postgresql = factories.postgresql("postgresql_proc")


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _apply_alembic_upgrade(url: str) -> None:
    """Run `alembic upgrade head` against the given URL.

    Uses the Alembic Python API directly so test failures attribute
    properly (stack trace points at the migration, not at a
    CalledProcessError from a subprocess).

    Bypasses env.py's config.yaml loading by setting sqlalchemy.url
    directly on the config — env.py honors this when the
    `test_mode` -x flag is set.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig

    alembic_cfg = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    alembic_cfg.cmd_opts = type("X", (), {"x": ["test_mode=1"]})()
    command.upgrade(alembic_cfg, "head")


@pytest.fixture
def db_engine(postgresql) -> Engine:
    """Provide a SQLAlchemy Engine connected to a fresh, migrated DB.

    Each test gets its own database (pytest-postgresql function scope),
    so there's no inter-test pollution.
    """
    conn_info = postgresql.info
    url = (
        f"postgresql+psycopg2://{conn_info.user}@"
        f"{conn_info.host}:{conn_info.port}/{conn_info.dbname}"
    )
    _apply_alembic_upgrade(url)
    engine = create_engine(url, future=True)
    yield engine
    engine.dispose()
