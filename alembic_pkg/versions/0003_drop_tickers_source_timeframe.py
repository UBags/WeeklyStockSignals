"""drop tickers.source_timeframe column

The column was added in legacy migration 007 to record the resolution
at which the ETL writes bars per ticker. In practice the ETL never
kept it in sync — refresh-listings inserts new ticker rows with the
schema default '1W' and backfill writes '1D' bars without updating
the column. Every reader that trusted the column got stale data.

Stages 5.1 and 5.2 of the Phase-13 cleanup replaced all production
reads of the column with discovery from ohlcv_bars itself (the only
source of truth about what bars actually exist for a ticker). With
no remaining readers, the column is dead metadata. This migration
removes it.

Revision ID: 0003
Revises: 0002_db_roles
Create Date: 2026-05-18

Downgrade restores the column with its original schema (NOT NULL,
DEFAULT '1W', plus the CHECK constraint validating the value set).
Existing rows after downgrade will all carry the default '1W',
which matches what would have happened pre-Phase-13 anyway since
nothing kept the column current.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0003_drop_source_timeframe"
down_revision = "0002_db_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The CHECK constraint is dropped automatically when the column
    # goes (Postgres cascades constraint removal on DROP COLUMN).
    op.drop_column("tickers", "source_timeframe")


def downgrade() -> None:
    # Restore the column with its original schema. Existing rows
    # get the server default ('1W') since the original data is gone.
    op.add_column(
        "tickers",
        sa.Column(
            "source_timeframe",
            sa.String(8),
            nullable=False,
            server_default="1W",
        ),
    )
    # And the CHECK constraint that was in 0001_initial_schema.
    op.create_check_constraint(
        "tickers_source_timeframe_chk",
        "tickers",
        "source_timeframe IN ('15m', '1h', '2h', '1D', '1W')",
    )