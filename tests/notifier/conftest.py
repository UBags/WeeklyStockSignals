"""Re-export the persistence DB fixture so notifier tests get a real DB."""
from tests.persistence.conftest import (   # noqa: F401
    db_engine,
    postgresql,
    postgresql_proc,
)
