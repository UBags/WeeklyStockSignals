"""Create industry_regime_corr: industry x industry regime correlation
matrix (spec §17.3). A single current snapshot (asof_date), refreshed each
backfill. best_lag<0 for (i,j) means j leads i.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0044_industry_regime_corr"
down_revision = "0043_sector_is_constituent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "industry_regime_corr",
        sa.Column("industry_i", sa.String(128), nullable=False),
        sa.Column("industry_j", sa.String(128), nullable=False),
        sa.Column("window", sa.Integer, nullable=False),
        sa.Column("asof_date", sa.Date, nullable=False),
        sa.Column("corr", sa.Float, nullable=False),
        sa.Column("best_lag", sa.Integer, nullable=False),
        sa.Column("lead_lag_corr", sa.Float, nullable=False),
        sa.Column("n_obs", sa.Integer, nullable=False),
        sa.Column(
            "computed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint(
            "industry_i", "industry_j", "window",
            name="industry_regime_corr_pkey",
        ),
    )
    op.create_index(
        "ix_irc_i", "industry_regime_corr", ["industry_i", "window"],
    )


def downgrade() -> None:
    op.drop_table("industry_regime_corr")
