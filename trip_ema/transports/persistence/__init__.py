"""Persistence layer — DB access via SQLAlchemy Core.

All access goes through repository classes that take a Connection in their
constructor and return domain objects. The caller controls the transaction
boundary using `connection.transaction(engine)`.
"""

from .bars_repo import BarsRepository
from .connection import (
    build_db_url,
    create_engine_from_config,
    transaction,
)
from .migrator import apply_pending
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
    "apply_pending",
    "build_db_url",
    "create_engine_from_config",
    "transaction",
]
