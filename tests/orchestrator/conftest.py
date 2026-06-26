"""conftest for tests/orchestrator/.

V1 cutover tests for orchestrator-package primitives (derive_signals,
rotate_signals, recompute_market, notify_subscribers) need the same
`db_engine` fixture used by the persistence tests: a fresh Postgres
database per test with Alembic applied through HEAD.

Same re-export pattern as tests/fundamentals/conftest.py and
tests/signals/conftest.py.
"""
from __future__ import annotations

from tests.persistence.conftest import (  # noqa: F401
    db_engine,
    postgresql,
    postgresql_proc,
)
