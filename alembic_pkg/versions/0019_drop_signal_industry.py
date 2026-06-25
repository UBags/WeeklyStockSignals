"""Drop industry shape from signal_events.

Final demolition step for industry-shape signal events. A2 (commit
b58157b) removed all code that wrote to or read from this column;
this migration removes the column from the schema and tightens the
two columns that were nullable only to support the polymorphic
shape.

What this migration changes on `signal_events`:
  - Drops the `industry_name VARCHAR(128)` column (was nullable;
    0 of 98,583 rows had a non-NULL value at A3 time).
  - Drops the `signal_events_series_xor_chk` CHECK constraint
    (enforced "either (ticker_id, timeframe) set OR industry_name
    set; never both, never neither").
  - Drops the `signal_events_industry_name_fkey` foreign key
    (referenced industry_index.industry_name).
  - Drops the partial unique index `uq_signal_events_industry`.
  - Tightens `ticker_id` to NOT NULL (was nullable for industry rows).
  - Tightens `timeframe` to NOT NULL (same reason).
  - Recreates `uq_signal_events_ticker` without its `WHERE
    ticker_id IS NOT NULL` predicate. After ticker_id becomes NOT
    NULL, the predicate is vacuous; dropping it lets the upsert
    code use a plain ON CONFLICT clause without a matching WHERE.

Deploy ordering — IMPORTANT:
  - This migration assumes NO application code still writes
    industry_name on signal_events insert, NO test still expects
    the column or the XOR check, and the upsert SQL has been
    updated to match the recreated full index.
  - A2 (commit b58157b) made all those changes. The matching
    upsert code change (drop `WHERE ticker_id IS NOT NULL` from
    ON CONFLICT) ships in the SAME commit as this migration.

Downgrade semantics:
  - Drops NOT NULL on ticker_id and timeframe.
  - Recreates the partial unique index uq_signal_events_ticker
    with `WHERE ticker_id IS NOT NULL`.
  - Re-adds the `industry_name VARCHAR(128)` column (nullable).
  - Re-creates the partial unique index uq_signal_events_industry
    `(industry_name, bar_timestamp, signal_type) WHERE
    industry_name IS NOT NULL`.
  - Re-creates the foreign key to industry_index(industry_name)
    with ON DELETE CASCADE.
  - Re-creates the XOR CHECK constraint.
  - NOTE: cannot recover dropped data; the column was 100% NULL
    pre-drop, so nothing is lost. Industry-shape signal events
    were removed in the A1 demolition (commit e8c8c64); this just
    finalizes the schema cleanup.

Revision ID: 0019_drop_signal_industry
Revises: 0018_collapse_regime_columns
Create Date: 2026-06-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0019_drop_signal_industry"
down_revision = "0018_collapse_regime_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the CHECK constraint first — once dropped, ticker_id /
    #    timeframe can be made NOT NULL without the constraint
    #    objecting on industry rows (there are none, but defensive).
    op.drop_constraint(
        "signal_events_series_xor_chk", "signal_events", type_="check",
    )

    # 2. Drop the FK to industry_index. Must come before column drop.
    op.drop_constraint(
        "signal_events_industry_name_fkey",
        "signal_events", type_="foreignkey",
    )

    # 3. Drop the partial unique index on industry_name.
    op.drop_index("uq_signal_events_industry", table_name="signal_events")

    # 4. Drop the column itself.
    op.drop_column("signal_events", "industry_name")

    # 5. Tighten ticker_id and timeframe to NOT NULL. Verified at
    #    A3 time: 0 of 98,583 rows have NULL on either column.
    op.alter_column(
        "signal_events", "ticker_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "signal_events", "timeframe",
        existing_type=sa.String(),
        nullable=False,
    )

    # 6. Recreate uq_signal_events_ticker without the partial-where.
    #    Postgres requires the ON CONFLICT clause to syntactically
    #    match the target index's WHERE predicate; with ticker_id
    #    now NOT NULL, the predicate is vacuous and we drop it so
    #    the upsert ON CONFLICT no longer needs the WHERE clause.
    op.drop_index("uq_signal_events_ticker", table_name="signal_events")
    op.create_index(
        "uq_signal_events_ticker", "signal_events",
        ["ticker_id", "timeframe", "bar_timestamp", "signal_type"],
        unique=True,
    )


def downgrade() -> None:
    # Reverse the index recreation: drop the full unique index,
    # add back the partial-where version.
    op.drop_index("uq_signal_events_ticker", table_name="signal_events")
    op.create_index(
        "uq_signal_events_ticker", "signal_events",
        ["ticker_id", "timeframe", "bar_timestamp", "signal_type"],
        unique=True,
        postgresql_where=sa.text("ticker_id IS NOT NULL"),
    )

    # Drop NOT NULL on the two tightened columns.
    op.alter_column(
        "signal_events", "timeframe",
        existing_type=sa.String(),
        nullable=True,
    )
    op.alter_column(
        "signal_events", "ticker_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    # Re-add industry_name column.
    op.add_column(
        "signal_events",
        sa.Column("industry_name", sa.String(length=128), nullable=True),
    )

    # Re-add the partial unique index on industry_name.
    op.create_index(
        "uq_signal_events_industry", "signal_events",
        ["industry_name", "bar_timestamp", "signal_type"],
        unique=True,
        postgresql_where=sa.text("industry_name IS NOT NULL"),
    )

    # Re-add the FK to industry_index.
    op.create_foreign_key(
        "signal_events_industry_name_fkey",
        "signal_events", "industry_index",
        ["industry_name"], ["industry_name"],
        ondelete="CASCADE",
    )

    # Re-add the XOR CHECK constraint.
    op.create_check_constraint(
        "signal_events_series_xor_chk", "signal_events",
        "((ticker_id IS NOT NULL AND timeframe IS NOT NULL "
        "  AND industry_name IS NULL) "
        "OR (ticker_id IS NULL AND timeframe IS NULL "
        "  AND industry_name IS NOT NULL))",
    )
