"""conftest for tests/signals/.

V1 cutover tests (SignalsReader and later) need the same `db_engine`
fixture used by the persistence tests: a fresh Postgres database per
test with Alembic applied through HEAD. Rather than duplicate that
machinery, we re-export the persistence fixtures here.

`postgresql_proc` is session-scoped (one Postgres process for the
whole pytest run); `postgresql` is function-scoped (fresh DB per
test); `db_engine` wraps the function-scoped DB in a SQLAlchemy
Engine with Alembic upgraded to HEAD.

Existing pre-V1 tests in this directory (test_generate_signals.py,
test_persist_signals.py, etc.) are DB-free and don't use these
fixtures; they're unaffected.
"""
from __future__ import annotations

# Re-export the fixtures so they're discoverable in this dir.
from tests.persistence.conftest import (  # noqa: F401
    db_engine,
    postgresql,
    postgresql_proc,
)
