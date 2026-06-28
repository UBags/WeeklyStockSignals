"""Drop legacy signal_events table.

V1 cutover step 15b. The signal_events table's last code-side
readers and writers were demolished in commit 928ffbf (step 15a).
This revision drops the table itself.

Pre-conditions (verified before this revision applies):
  - No trip_ema/ module references signal_events as a SQL identifier
    in a live code path (15a verified).
  - notification_log.event_id FK still exists; we drop it explicitly
    before dropping the parent table.

Post-conditions:
  - signal_events table is gone.
  - notification_log.event_id column is KEPT (now an orphan integer;
    V1 writes NULL; pre-V1 rows keep their stale values). Removing
    the column would force changes to NotificationLogEntry/Payload
    schemas; back-compat over cleanliness.

Reversibility: schema-only. downgrade() recreates the table schema
(empty) and re-establishes the notification_log.event_id FK. Data is
NOT restored — recover from a database backup if needed. This is the
standard alembic pattern.

Revision ID: 0030_drop_legacy_signal_tables
Revises:     0029_compute_runs_mkt_overview
Created:     V1 cutover step 15b
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0030_drop_legacy_signal_tables"
down_revision = "0029_compute_runs_mkt_overview"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the FK constraint explicitly. DROP TABLE CASCADE would
    #    also handle it, but we prefer to be explicit about cross-table
    #    references.
    op.execute(
        "ALTER TABLE public.notification_log "
        "DROP CONSTRAINT IF EXISTS notification_log_event_id_fkey"
    )

    # 2. Drop signal_events. CASCADE handles dependent objects (indexes,
    #    sequences, constraint metadata) — but does NOT delete rows from
    #    other tables. notification_log rows are preserved; their event_id
    #    column now references a non-existent table (the column stays,
    #    just orphan).
    op.execute("DROP TABLE IF EXISTS public.signal_events CASCADE")


def downgrade() -> None:
    """Recreate the signal_events schema (empty) and restore the FK.

    Schema-only restoration. Data is not recovered — that requires a
    database backup. Mirrors the post-0019_drop_signal_industry state,
    since 0030 was the next migration after the 0019 column cleanups.
    """
    # 1. Recreate signal_events table with full schema.
    op.create_table(
        "signal_events",
        sa.Column(
            "event_id", sa.BigInteger,
            primary_key=True, autoincrement=True,
        ),
        sa.Column("ticker_id", sa.Integer, nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column(
            "bar_timestamp", sa.TIMESTAMP(timezone=True), nullable=False,
        ),
        sa.Column("signal_type", sa.String(length=16), nullable=False),
        sa.Column("price_at_signal", sa.Float, nullable=False),
        sa.Column(
            "computed_at", sa.TIMESTAMP(timezone=True), nullable=False,
        ),
        sa.Column("regime_before", sa.SmallInteger, nullable=True),
        sa.Column("regime_after", sa.SmallInteger, nullable=True),
        sa.CheckConstraint(
            "(regime_after IS NULL) OR (regime_after = ANY "
            "(ARRAY[-1, 0, 1]))",
            name="signal_events_regime_after_chk",
        ),
        sa.CheckConstraint(
            "(regime_before IS NULL) OR (regime_before = ANY "
            "(ARRAY[-1, 0, 1]))",
            name="signal_events_regime_before_chk",
        ),
        sa.CheckConstraint(
            "signal_type IN ('BUY', 'SELL', 'EXIT_FROM_BUY', 'EXIT_FROM_SELL')",
            name="signal_events_signal_type_check",
        ),
        sa.ForeignKeyConstraint(
            ["ticker_id"], ["tickers.ticker_id"],
            name="signal_events_ticker_id_fkey",
            ondelete="CASCADE",
        ),
    )

    # 2. Recreate indexes.
    op.create_index(
        "uq_signal_events_ticker", "signal_events",
        ["ticker_id", "timeframe", "bar_timestamp", "signal_type"],
        unique=True,
    )
    op.create_index(
        "idx_signal_events_bar_timestamp", "signal_events",
        [sa.text("bar_timestamp DESC")],
    )
    op.create_index(
        "idx_signal_events_computed", "signal_events",
        ["computed_at"],
    )
    op.create_index(
        "idx_signal_events_lookup", "signal_events",
        ["ticker_id", sa.text("computed_at DESC")],
    )
    op.create_index(
        "idx_signal_events_signal_type_ts", "signal_events",
        ["signal_type", sa.text("bar_timestamp DESC")],
    )

    # 3. Restore the FK from notification_log.event_id to
    #    signal_events.event_id with ON DELETE CASCADE.
    op.create_foreign_key(
        "notification_log_event_id_fkey",
        source_table="notification_log",
        referent_table="signal_events",
        local_cols=["event_id"],
        remote_cols=["event_id"],
        ondelete="CASCADE",
    )
