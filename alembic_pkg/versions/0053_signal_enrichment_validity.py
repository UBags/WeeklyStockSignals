"""Add data_is_valid, bars_available, day_of_week to signal_enrichment.

Additive. 0052 built the 99 band columns nullable; this adds three more and
does NOT tighten any of them — the strict-literal data_is_valid flag replaces
the NOT NULL tightening that an earlier plan intended, and it does the same
job (filter warm-up / broken rows out of ML) without failing the migration
on legacy rows.

  data_is_valid  boolean NOT NULL DEFAULT false
                 Guilty until proven innocent: any legacy or half-written row
                 reads false. The v0.5 recompute sets it true only for rows
                 whose every band column is non-NULL (see the repo).

  bars_available integer NULL
                 Weekly bars behind the signal bar. NULL on pre-v0.5 rows.

  day_of_week    integer NOT NULL DEFAULT 1
                 ISO weekday of signal_date, Mon=1..Sun=7. The default exists
                 only so the NOT NULL can be added without a table rewrite on
                 legacy rows; the recompute overwrites every row with the
                 real value, and a CHECK keeps it in range.

Revision ID: 0053_signal_enrichment_validity
Revises:     0052_signal_enrichment_bands
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0053_signal_enrichment_validity"
down_revision = "0052_signal_enrichment_bands"
branch_labels = None
depends_on = None

TABLE = "signal_enrichment"
DOW_CHK = "se_day_of_week_chk"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("data_is_valid", sa.Boolean, nullable=False,
                  server_default=sa.false()),
    )
    op.add_column(TABLE, sa.Column("bars_available", sa.Integer,
                                   nullable=True))
    op.add_column(
        TABLE,
        sa.Column("day_of_week", sa.Integer, nullable=False,
                  server_default="1"),
    )
    op.create_check_constraint(
        DOW_CHK, TABLE, "day_of_week BETWEEN 1 AND 7",
    )

    op.execute(
        "COMMENT ON COLUMN signal_enrichment.data_is_valid IS "
        "'True iff EVERY band column on this row is non-NULL. Strict: because "
        "atr30/atr40 are NULL until ~40 weeks of history, this is in practice "
        "core-present AND >=~40 weeks. The one-clause ML filter. Use "
        "bars_available for models needing only the short ATR families.'"
    )
    op.execute(
        "COMMENT ON COLUMN signal_enrichment.bars_available IS "
        "'Weekly bars the assembler had seen at the signal bar. The honest "
        "test of whether the long-period ATRs mean anything.'"
    )
    op.execute(
        "COMMENT ON COLUMN signal_enrichment.day_of_week IS "
        "'ISO weekday of signal_date: Monday=1 .. Sunday=7.'"
    )


def downgrade() -> None:
    op.drop_constraint(DOW_CHK, TABLE, type_="check")
    op.drop_column(TABLE, "day_of_week")
    op.drop_column(TABLE, "bars_available")
    op.drop_column(TABLE, "data_is_valid")
