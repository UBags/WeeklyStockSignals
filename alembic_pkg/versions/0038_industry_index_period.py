"""Create industry_index_period: per-rebalance chain-link factors.

Part of the chain-linked industry-index rebuild. The index value model is
changing from "reset to base_value (1000) at every rebalance" to a continuous
chain-linked series: at each rebalance we splice the new basket onto the
existing index level so the index does not jump when constituents change.

The splice needs a per-rebalance-period scalar (link_factor) that replaces the
constant base_value in compute_index_value_for_date:

    value(t) = link_factor(period)
               × Σ_i_present [ (weight_i / Σ_j weight_j)
                               × close_i(t) / close_i(rebalance_date) ]

For the FIRST (base) rebalance: link_factor = base_value (index starts at 1000).
For every SUBSEQUENT rebalance on date R:
    link_factor = old_index_value(R-1) / new_basket_ratiosum(R-1)
which makes the new basket's value on R-1 exactly equal the old index level,
i.e. no jump. prior_value and new_basket_ratiosum are stored alongside for
auditability.

One row per (industry_name, rebalance_date). compute_index_value_for_date looks
up the factor by the effective_rebalance_date it already resolves.

Downgrade drops the table (functional, for test round-trips; operational policy
remains roll-forward-only).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0038_industry_index_period"
down_revision = "0037_signal_date_not_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "industry_index_period",
        sa.Column("industry_name", sa.String(128), nullable=False),
        sa.Column("rebalance_date", sa.Date, nullable=False),
        sa.Column(
            "link_factor", sa.Float(precision=53), nullable=False,
        ),
        sa.Column(
            "is_base", sa.Boolean,
            nullable=False, server_default=sa.text("FALSE"),
        ),
        # prior_value / new_basket_ratiosum are NULL for the base period
        # (no splice performed) and populated for chained periods.
        sa.Column("prior_value", sa.Float(precision=53), nullable=True),
        sa.Column(
            "new_basket_ratiosum", sa.Float(precision=53), nullable=True,
        ),
        sa.Column(
            "computed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint(
            "industry_name", "rebalance_date",
            name="industry_index_period_pkey",
        ),
        sa.CheckConstraint(
            "link_factor > 0",
            name="iip_link_factor_chk",
        ),
    )
    # Value computation looks up the factor for a resolved
    # effective_rebalance_date within an industry.
    op.create_index(
        "ix_industry_index_period_lookup",
        "industry_index_period", ["industry_name", "rebalance_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_industry_index_period_lookup",
        table_name="industry_index_period",
    )
    op.drop_table("industry_index_period")
