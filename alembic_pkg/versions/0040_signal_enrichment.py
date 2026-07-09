"""Create signal_enrichment: per-signal ML feature store (spec v0.3 §19).

The signal-quality model (Bad / Good / Very Good) predicts *per signal*,
so features are materialised only at signal bars — ~45K rows, not the
~6.7M of the daily grain. Storing the 217-wide vector daily for every
stock would add ~12 GB; per-signal it is well under 100 MB.

`features` holds the evolving Appendix-A vector as JSONB: during the
design phase (v0.3) features are still being added/pruned, and a JSONB
column absorbs that without a migration per change, while training loads
the whole vector into pandas in one shot. The stable bits — signal
identity, industry context, forward-return labels (the target y, filled
by a later offline pass), and the feature-spec version for provenance —
are typed columns.

Natural key is (ticker_id, timeframe, signal_date, signal_type), matching
historical_signals; no FK to historical_signals.signal_id so the store
can be rebuilt independently of signal-row identity.

Downgrade drops the table (indexes cascade); operational policy remains
roll-forward-only.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0040_signal_enrichment"
down_revision = "0039_pipeline_phase_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signal_enrichment",
        sa.Column(
            "signal_enrichment_id", sa.BigInteger,
            sa.Identity(), nullable=False,
        ),
        # --- signal identity (natural key, aligns with historical_signals) ---
        sa.Column("ticker_id", sa.Integer, nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("signal_date", sa.Date, nullable=False),
        sa.Column("signal_type", sa.String(16), nullable=False),
        # --- denormalised context for grouping / sector joins ---
        sa.Column("industry_id", sa.Integer, nullable=True),
        # --- the evolving Appendix-A feature vector (§4 + structure + §16/§17) ---
        sa.Column("features", postgresql.JSONB, nullable=False),
        sa.Column("feature_spec_version", sa.String(16), nullable=False),
        # --- labels: target y, filled by a later offline pass (NULL until) ---
        sa.Column("fwd_ret_4w", sa.Float, nullable=True),
        sa.Column("fwd_ret_8w", sa.Float, nullable=True),
        sa.Column("fwd_ret_13w", sa.Float, nullable=True),
        sa.Column("fwd_max_dd", sa.Float, nullable=True),
        sa.Column("quality_class", sa.String(16), nullable=True),
        sa.Column(
            "computed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint(
            "signal_enrichment_id", name="signal_enrichment_pkey",
        ),
        sa.UniqueConstraint(
            "ticker_id", "timeframe", "signal_date", "signal_type",
            name="signal_enrichment_signal_uq",
        ),
        sa.CheckConstraint(
            "signal_type IN "
            "('BUY', 'SELL', 'EXIT_FROM_BUY', 'EXIT_FROM_SELL')",
            name="se_signal_type_chk",
        ),
        sa.CheckConstraint(
            "quality_class IS NULL OR "
            "quality_class IN ('BAD', 'GOOD', 'VERY_GOOD')",
            name="se_quality_class_chk",
        ),
    )
    op.create_index(
        "ix_signal_enrichment_ticker_date", "signal_enrichment",
        ["ticker_id", "signal_date"],
    )
    op.create_index(
        "ix_signal_enrichment_industry", "signal_enrichment",
        ["industry_id"],
    )
    op.create_index(
        "ix_signal_enrichment_quality", "signal_enrichment",
        ["quality_class"],
    )


def downgrade() -> None:
    op.drop_table("signal_enrichment")
