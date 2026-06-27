"""conftest for tests/notifications/.

`tests/notifications/` exists with several legacy test files but
no conftest. The V1 compose_email tests need the same `db_engine`
fixture that the persistence tests use — same re-export pattern as
tests/fundamentals/conftest.py, tests/signals/conftest.py, and
tests/orchestrator/conftest.py.
"""
from __future__ import annotations

from tests.persistence.conftest import (  # noqa: F401
    db_engine,
    postgresql,
    postgresql_proc,
)
