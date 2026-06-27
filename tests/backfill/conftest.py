"""conftest for tests/backfill/.

Step 11b1's pipeline tests need a real Postgres `db_engine` to
exercise the 10-stage orchestration against actual schema. Same
re-export pattern as tests/orchestrator/conftest.py.
"""
from __future__ import annotations

from tests.persistence.conftest import (  # noqa: F401
    db_engine,
    postgresql,
    postgresql_proc,
)
