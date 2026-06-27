"""conftest for tests/app/.

Step 11a's deferral-layer tests need a real Postgres `db_engine` to
exercise `BackfillRunsRepository.is_running()` against actual marker
rows. Same re-export pattern used by tests/orchestrator/conftest.py,
tests/notifications/conftest.py, etc.

The pre-existing tests/app/test_v1_factories.py (Step 10) and
test_build_orchestrator_components.py don't use db_engine — they
pass MagicMock() — so this conftest was not previously needed.
Adding it is additive: existing tests still pass through unchanged.
"""
from __future__ import annotations

from tests.persistence.conftest import (  # noqa: F401
    db_engine,
    postgresql,
    postgresql_proc,
)
