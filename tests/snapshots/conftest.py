"""conftest for tests/snapshots/.

Step 13a: re-exports db_engine so the snapshot tests can use the
shared pytest-postgresql + Alembic-migrated ephemeral DB. Same
shim pattern as tests/backfill/conftest.py and
tests/transports/http/routes/conftest.py.
"""
from __future__ import annotations

from tests.persistence.conftest import (  # noqa: F401
    db_engine,
    postgresql,
    postgresql_proc,
)
