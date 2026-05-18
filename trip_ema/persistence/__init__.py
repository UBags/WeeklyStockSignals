"""Persistence layer — DB access via SQLAlchemy Core.

All access goes through repository classes that take a Connection in their
constructor and return domain objects. The caller controls the transaction
boundary using `connection.transaction(engine)`.

Phase 12: the legacy SQL migrator (`migrator.apply_pending`) is no
longer exported from this package. It still exists at
`trip_ema.persistence.migrator` as a module so the historical regression
tests can import it directly, but it's no longer part of the supported
public API — `app.py` uses Alembic exclusively via the bridge.
"""

from .bars_repo import BarsRepository
from .connection import (
    build_db_url,
    create_engine_from_config,
    transaction,
)
from .signals_repo import SignalsRepository
from .subscribers_repo import (
    SubscriberRecord,
    SubscribersRepository,
    SubscriptionRecord,
)
from .tickers_repo import TickerRecord, TickersRepository

__all__ = [
    "BarsRepository",
    "SignalsRepository",
    "SubscriberRecord",
    "SubscribersRepository",
    "SubscriptionRecord",
    "TickerRecord",
    "TickersRepository",
    "build_db_url",
    "create_engine_from_config",
    "transaction",
]
