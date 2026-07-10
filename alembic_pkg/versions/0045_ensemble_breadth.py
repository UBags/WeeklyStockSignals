"""Create industry_regime_breadth + influential_industry_ensemble (§17.4).

breadth: the per-industry weekly regime proxy g^I (stored so §17c can look
up an ensemble member's regime at a signal's week). ensemble: each
industry's influential peers under three (theta, M) variants.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0045_ensemble_breadth"
down_revision = "0044_industry_regime_corr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "industry_regime_breadth",
        sa.Column("industry_name", sa.String(128), nullable=False),
        sa.Column("asof_date", sa.Date, nullable=False),
        sa.Column("breadth", sa.Float, nullable=False),
        sa.Column(
            "computed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint(
            "industry_name", "asof_date",
            name="industry_regime_breadth_pkey",
        ),
    )
    op.create_table(
        "influential_industry_ensemble",
        sa.Column("industry_i", sa.String(128), nullable=False),
        sa.Column("member_j", sa.String(128), nullable=False),
        sa.Column("variant", sa.String(16), nullable=False),
        sa.Column("corr", sa.Float, nullable=False),
        sa.Column("best_lag", sa.Integer, nullable=False),
        sa.Column("is_leader", sa.Boolean, nullable=False),
        sa.Column("sign", sa.Integer, nullable=False),
        sa.Column("asof_date", sa.Date, nullable=False),
        sa.Column(
            "computed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint(
            "industry_i", "member_j", "variant",
            name="influential_industry_ensemble_pkey",
        ),
    )
    op.create_index(
        "ix_iie_i_variant", "influential_industry_ensemble",
        ["industry_i", "variant"],
    )


def downgrade() -> None:
    op.drop_table("influential_industry_ensemble")
    op.drop_table("industry_regime_breadth")
