"""Database connection management.

Single source of truth for engine creation. Repositories take a Connection
or Session in their constructor — they don't create their own. This makes
testing trivial (inject a test connection) and keeps transaction control
at the call-site, where it belongs.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import Connection

from ..config import Config, resolve_db_password


def build_db_url(config: Config) -> str:
    """Build the SQLAlchemy URL with the password resolved from env."""
    pwd = resolve_db_password(config)
    db = config.database
    return f"postgresql+psycopg2://{db.user}:{pwd}@{db.host}:{db.port}/{db.database}"


def create_engine_from_config(config: Config, **engine_kwargs) -> Engine:
    """Create a SQLAlchemy engine from validated config.

    Pool settings come from `config.database.pool_*` fields. Callers can
    override via `engine_kwargs` (e.g., `pool_size=1` for a test).
    `pool_pre_ping=True` is always on so we recover from stale
    connections without needing the application to handle a transient
    OperationalError on the first query after a network blip.
    """
    url = build_db_url(config)
    db = config.database
    pool_kwargs = {
        "pool_pre_ping": True,
        "pool_size": db.pool_size,
        "max_overflow": db.pool_max_overflow,
        "pool_timeout": db.pool_timeout_seconds,
        "pool_recycle": db.pool_recycle_seconds,
    }
    # Caller overrides take priority — useful in tests where the kwargs
    # would otherwise conflict with NullPool / StaticPool fixtures.
    pool_kwargs.update(engine_kwargs)
    return create_engine(url, **pool_kwargs)


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    """Yield a connection inside a transaction. Commits on clean exit,
    rolls back on exception. Standard pattern; use this everywhere a write
    is involved.

    Example:
        with transaction(engine) as conn:
            repo = TickersRepository(conn)
            repo.insert(...)
    """
    with engine.begin() as conn:
        yield conn
