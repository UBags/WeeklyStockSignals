"""Add is_constituent to sector_stock_leadlag (spec §16.4).

§16 carries whether the stock was an index constituent (weight>0) at the
asof week, so the model can weight constituents vs non-constituents
differently. Nullable — populated by the §16 lead/lag pass; rs_rank rows
(window=4) leave it NULL.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0043_sector_is_constituent"
down_revision = "0042_sector_stock_leadlag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sector_stock_leadlag",
        sa.Column("is_constituent", sa.Boolean, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sector_stock_leadlag", "is_constituent")
