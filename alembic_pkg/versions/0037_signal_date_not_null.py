"""historical_signals.signal_date -> NOT NULL (rotation removed)

Revision ID: 0037_signal_date_not_null
Revises: 0036_hist_signals_per_day
Create Date: 2026-07-06

0036 added signal_date as NULLABLE so the legacy rotation primitive
(rotate_current_to_historical), which INSERTed into historical_signals
without a signal_date, kept working during the transition. Rotation is now
removed — every writer of historical_signals goes through
process_signals_for_day, which always sets signal_date via derive. So the
column can be tightened to NOT NULL.

Guard: if any NULL signal_date rows exist (e.g. a stray legacy row), the
ALTER would fail with a bare Postgres error. We check first and raise a
clear message instead, so the operator knows exactly what to fix.

Downgrade reverses to NULLABLE (functional, for test round-trips; operational
policy remains roll-forward-only).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0037_signal_date_not_null"
down_revision = "0036_hist_signals_per_day"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    n_null = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM historical_signals WHERE signal_date IS NULL"
        )
    ).scalar_one()
    if n_null:
        raise RuntimeError(
            f"Cannot set historical_signals.signal_date NOT NULL: "
            f"{n_null} row(s) have NULL signal_date. These predate the "
            f"per-day model (legacy rotation). Backfill or delete them "
            f"first, then re-run this migration."
        )
    op.alter_column(
        "historical_signals", "signal_date",
        existing_type=sa.Date(), nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "historical_signals", "signal_date",
        existing_type=sa.Date(), nullable=True,
    )
