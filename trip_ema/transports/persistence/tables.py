"""SQLAlchemy Core Table definitions.

Mirrors the migration SQL. The Table objects are used by the repos to
build queries; they are NOT used to create the schema (the migrator
applies the SQL files directly). Keeping the two in sync is a manual
discipline — any migration change should be reflected here.

Why not declarative_base / ORM? Because we want the repos to return
domain objects, not ORM-bound instances. SQLAlchemy Core gives us
parameterized SQL and result-set iteration without the ORM's complexity.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()


tickers = Table(
    "tickers",
    metadata,
    Column("ticker_id", Integer, primary_key=True),
    Column("exchange", String(16), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("instrument", String(16), nullable=False, server_default="EQ"),
    Column("full_name", String(128)),
    Column("is_active", Boolean, nullable=False, server_default="TRUE"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Phase 8 B4 — exchange-local timezone for week-anchor computation
    # and source-resolution timeframe for the ETL.
    Column("tz", String(32), nullable=False, server_default="UTC"),
    Column("source_timeframe", String(8), nullable=False, server_default="1W"),
    UniqueConstraint("exchange", "symbol", "instrument", name="tickers_uq"),
)


ohlcv_bars = Table(
    "ohlcv_bars",
    metadata,
    Column("ticker_id", Integer, ForeignKey("tickers.ticker_id"), primary_key=True),
    Column("timeframe", String(8), primary_key=True),
    Column("bar_timestamp", DateTime(timezone=True), primary_key=True),
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("volume", Float, nullable=False),
    # Added in migration 006 — auto-set on INSERT and on UPDATE (via trigger).
    # Used by the ComputeScheduler to detect historical revisions.
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


signal_events = Table(
    "signal_events",
    metadata,
    Column("event_id", BigInteger, primary_key=True),
    Column("ticker_id", Integer, ForeignKey("tickers.ticker_id"), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("bar_timestamp", DateTime(timezone=True), nullable=False),
    Column("signal_type", String(16), nullable=False),
    Column("price_at_signal", Float, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "signal_type IN ('BUY', 'SELL', 'EXIT_FROM_BUY', 'EXIT_FROM_SELL')",
        name="signal_events_signal_type_chk",
    ),
)


# ticker_signal_state was dropped in migration 005.


subscribers = Table(
    "subscribers",
    metadata,
    Column("subscriber_id", Integer, primary_key=True),
    Column("transport", String(16), nullable=False),
    Column("address", Text, nullable=False),
    Column("notify_cadence_minutes", Integer, nullable=False),
    Column("last_notified_at", DateTime(timezone=True)),
    Column("is_active", Boolean, nullable=False, server_default="TRUE"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "transport IN ('email', 'webhook', 'slack', 'sms')",
        name="subscribers_transport_chk",
    ),
    CheckConstraint(
        "notify_cadence_minutes BETWEEN 15 AND 1440 "
        "AND notify_cadence_minutes % 15 = 0",
        name="subscribers_cadence_chk",
    ),
)


subscriptions = Table(
    "subscriptions",
    metadata,
    Column("subscription_id", Integer, primary_key=True),
    Column(
        "subscriber_id", Integer,
        ForeignKey("subscribers.subscriber_id"), nullable=False,
    ),
    # NULL ticker_id means "all tickers"
    Column("ticker_id", Integer, ForeignKey("tickers.ticker_id")),
    Column("signal_filter", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


notification_log = Table(
    "notification_log",
    metadata,
    # Partitioned tables in Postgres need the partition key in the PK.
    Column("log_id", BigInteger, primary_key=True, autoincrement=True),
    Column("last_sent_at", DateTime(timezone=True), primary_key=True),
    Column(
        "subscriber_id", Integer,
        ForeignKey("subscribers.subscriber_id"), nullable=False,
    ),
    Column(
        "event_id", BigInteger,
        ForeignKey("signal_events.event_id"), nullable=False,
    ),
    Column("notification_count", Integer, nullable=False, server_default="1"),
)
