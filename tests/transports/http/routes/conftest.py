"""conftest for tests/transports/http/routes/.

Step 12a fixup: re-exports db_engine so test_signal_history.py
can use the shared pytest-postgresql + Alembic-migrated ephemeral
DB instead of its homegrown DB-creation logic. Same shim pattern
as tests/backfill/conftest.py.
"""
from __future__ import annotations

from tests.persistence.conftest import (  # noqa: F401
    db_engine,
    postgresql,
    postgresql_proc,
)
