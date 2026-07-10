"""Split labels into signal_labels; drop the orphaned label columns.

Performance labels (forward returns, max drawdown, quality class) are
retrospective: they need bars AFTER the signal, so recent signals cannot
carry them and today's signal is unlabellable for a quarter. Keeping them
on signal_enrichment forced NULLs on every recent row and invited leakage
(a SELECT * at inference pulling target columns). This moves them to a
dedicated signal_labels table, 1:1 with signal_enrichment via FK, so the
causal boundary is physical: signal_enrichment = X (always complete),
signal_labels = y (grows as the future arrives, joined only for training).

Also carries fwd_rr (reward in ATR units) and horizon_complete (whether
all 13 forward weeks existed, i.e. the label is final vs provisional).

Roll-forward-only in practice; downgrade restores the columns for safety.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0041_signal_labels"
down_revision = "0040_signal_enrichment"
branch_labels = None
depends_on = None

_LABEL_COLS = ("fwd_ret_4w", "fwd_ret_8w", "fwd_ret_13w",
               "fwd_max_dd", "quality_class")


def upgrade() -> None:
    op.create_table(
        "signal_labels",
        sa.Column(
            "signal_label_id", sa.BigInteger,
            sa.Identity(), nullable=False,
        ),
        sa.Column("signal_enrichment_id", sa.BigInteger, nullable=False),
        # --- forward-return labels (target y) ---
        sa.Column("fwd_ret_4w", sa.Float, nullable=True),
        sa.Column("fwd_ret_8w", sa.Float, nullable=True),
        sa.Column("fwd_ret_13w", sa.Float, nullable=True),
        sa.Column("fwd_max_dd", sa.Float, nullable=True),
        sa.Column("fwd_rr", sa.Float, nullable=True),
        sa.Column("quality_class", sa.String(16), nullable=True),
        # true once all 13 forward weeks existed (label is final).
        sa.Column(
            "horizon_complete", sa.Boolean,
            nullable=False, server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "computed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint(
            "signal_label_id", name="signal_labels_pkey",
        ),
        sa.ForeignKeyConstraint(
            ["signal_enrichment_id"],
            ["signal_enrichment.signal_enrichment_id"],
            name="signal_labels_se_fk", ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "signal_enrichment_id", name="signal_labels_se_uq",
        ),
        sa.CheckConstraint(
            "quality_class IS NULL OR "
            "quality_class IN ('BAD', 'GOOD', 'VERY_GOOD')",
            name="sl_quality_class_chk",
        ),
    )
    op.create_index(
        "ix_signal_labels_quality", "signal_labels", ["quality_class"],
    )

    # drop the orphaned label columns from signal_enrichment
    op.drop_index(
        "ix_signal_enrichment_quality", table_name="signal_enrichment",
    )
    op.drop_constraint(
        "se_quality_class_chk", "signal_enrichment", type_="check",
    )
    for col in _LABEL_COLS:
        op.drop_column("signal_enrichment", col)


def downgrade() -> None:
    op.add_column(
        "signal_enrichment",
        sa.Column("fwd_ret_4w", sa.Float, nullable=True),
    )
    op.add_column(
        "signal_enrichment",
        sa.Column("fwd_ret_8w", sa.Float, nullable=True),
    )
    op.add_column(
        "signal_enrichment",
        sa.Column("fwd_ret_13w", sa.Float, nullable=True),
    )
    op.add_column(
        "signal_enrichment",
        sa.Column("fwd_max_dd", sa.Float, nullable=True),
    )
    op.add_column(
        "signal_enrichment",
        sa.Column("quality_class", sa.String(16), nullable=True),
    )
    op.create_check_constraint(
        "se_quality_class_chk", "signal_enrichment",
        "quality_class IS NULL OR "
        "quality_class IN ('BAD', 'GOOD', 'VERY_GOOD')",
    )
    op.create_index(
        "ix_signal_enrichment_quality", "signal_enrichment",
        ["quality_class"],
    )
    op.drop_table("signal_labels")
