"""Read-only DB session for the PySide GUI.

The GUI talks to Postgres directly for view queries (per Slice 2
architecture decision). Writes go through HTTP to the main service.

Two reasons we keep a separate session class rather than reusing the
trip_ema engine:
  - The GUI process is separate from the main service; they don't
    share the AppState SQLAlchemy engine.
  - Connection-pool tuning is different: the GUI does short-lived
    reads on a 5s/30s cadence, the main service has long-lived
    pool of transaction connections.

Thread safety: each query gets its own short-lived connection from
the pool, opened and closed in a context manager. The pool itself
is thread-safe (SQLAlchemy's default).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

from ..config import Config
from ..config.loader import resolve_db_password

logger = logging.getLogger(__name__)


class GuiDbSession:
    """Tiny wrapper around a SQLAlchemy engine. Use `.connect()` as a
    context manager for one short-lived query (or a few related ones).

    Construct ONE of these per GUI process. Pass it to any view that
    needs DB reads.
    """

    def __init__(self, config: Config, *, pool_size: int = 3):
        password = resolve_db_password(config)
        db = config.database
        url = (
            f"postgresql+psycopg2://{db.user}:{password}"
            f"@{db.host}:{db.port}/{db.database}"
        )
        self._engine: Engine = create_engine(
            url,
            pool_size=pool_size,
            max_overflow=2,
            pool_pre_ping=True,
            pool_recycle=1800,
        )

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        """Open a connection; close it on context exit. Read-only by
        convention — the GUI doesn't write via this path."""
        conn = self._engine.connect()
        try:
            yield conn
        finally:
            conn.close()

    def dispose(self) -> None:
        """Tear down the connection pool. Call once at GUI shutdown."""
        self._engine.dispose()
