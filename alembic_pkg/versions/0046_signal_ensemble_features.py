"""Create signal_ensemble_features (spec §17.5).

Per-signal ensemble features under each ensemble variant (floor/mid/
strict). Joined at read-time by signal_enrichment_id; FK cascades on
signal deletion.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0046_signal_ensemble_features"
down_revision = "0045_ensemble_breadth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_ensemble_features",
        sa.Column("signal_enrichment_id", sa.BigInteger, nullable=False),
        sa.Column("variant", sa.String(16), nullable=False),
        sa.Column("ensemble_score", sa.Float, nullable=False),
        sa.Column("ensemble_confirms", sa.Boolean, nullable=False),
        sa.Column("ensemble_agreement", sa.Float, nullable=False),
        sa.Column("n_leaders_bull", sa.Integer, nullable=False),
        sa.Column("n_leaders_bear", sa.Integer, nullable=False),
        sa.Column(
            "computed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["signal_enrichment_id"],
            ["signal_enrichment.signal_enrichment_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "signal_enrichment_id", "variant",
            name="signal_ensemble_features_pkey",
        ),
    )
    op.create_index(
        "ix_sef_variant", "signal_ensemble_features", ["variant"],
    )


def downgrade() -> None:
    op.drop_table("signal_ensemble_features")
