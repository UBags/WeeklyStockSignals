"""Create sector_stock_leadlag: per ticker x week sector-relative metrics.

Tier-3 cross-sectional analytics (spec §16.4), recomputed weekly per
industry rather than per-signal. rs_rank (§11b) lands now; the §16 sector
lead/lag columns are created nullable for the follow-up pass. Consumed by
JOIN on (ticker_id, asof_date, window) at training/analysis time — it is
deliberately NOT part of the per-signal features JSONB, whose cadence is
the signal, not the week.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0042_sector_stock_leadlag"
down_revision = "0041_signal_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sector_stock_leadlag",
        sa.Column("ticker_id", sa.Integer, nullable=False),
        sa.Column("asof_date", sa.Date, nullable=False),
        sa.Column("window", sa.Integer, nullable=False),
        sa.Column("industry_name", sa.String(128), nullable=False),
        sa.Column("rs_rank", sa.Float, nullable=True),
        # --- §16 sector lead/lag (filled by a later pass; nullable now) ---
        sa.Column("insync_corr", sa.Float, nullable=True),
        sa.Column("best_lag", sa.Integer, nullable=True),
        sa.Column("lead_lag_corr", sa.Float, nullable=True),
        sa.Column("beta_sector", sa.Float, nullable=True),
        sa.Column("coupling_class", sa.String(16), nullable=True),
        sa.Column("n_obs", sa.Integer, nullable=True),
        sa.Column(
            "computed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint(
            "ticker_id", "asof_date", "window",
            name="sector_stock_leadlag_pkey",
        ),
        sa.CheckConstraint(
            "coupling_class IS NULL OR coupling_class IN "
            "('LEADER', 'LAGGARD', 'COUPLED', 'DECOUPLED')",
            name="ssl_coupling_chk",
        ),
    )
    op.create_index(
        "ix_ssl_ticker_date", "sector_stock_leadlag",
        ["ticker_id", "asof_date"],
    )
    op.create_index(
        "ix_ssl_industry_date", "sector_stock_leadlag",
        ["industry_name", "asof_date"],
    )


def downgrade() -> None:
    op.drop_table("sector_stock_leadlag")
