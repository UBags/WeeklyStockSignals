"""Re-export the persistence DB fixture so registry/api tests can use it."""

# Importing the conftest from the persistence package is the simplest
# way to share fixtures across test modules. pytest picks up `db_engine`
# from this file when registry/api tests request it.

from tests.persistence.conftest import db_engine, postgresql, postgresql_proc

__all__ = ["db_engine", "postgresql", "postgresql_proc"]
