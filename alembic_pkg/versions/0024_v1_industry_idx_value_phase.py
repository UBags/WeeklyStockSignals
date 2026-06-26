"""Migration 0024 — extend fundamentals_run.phase CHECK constraint.

Adds `'v1_industry_index_value'` to the allowed values for
`fundamentals_run.phase`. Required by Step 6 of the clean-pipeline
V1 refactor — the new industry index value populator opens
fundamentals_run rows with that phase value.

Same shape as migrations 0022 and 0023.

Legacy parity: migrations/legacy/028_v1_industry_index_value_phase.sql.

Revision ID: 0024_v1_industry_index_value_phase
Revises:    0023_v1_stock_enrich_phase
Create Date: 2026-06-26
"""
from __future__ import annotations

from alembic import op


revision = "0024_v1_industry_idx_value_phase"
down_revision = "0023_v1_stock_enrich_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE fundamentals_run
            DROP CONSTRAINT IF EXISTS fundamentals_run_phase_chk;
    """)
    op.execute("""
        ALTER TABLE fundamentals_run
            ADD CONSTRAINT fundamentals_run_phase_chk
                CHECK (phase IN (
                    'industry', 'shares', 'financials',
                    'market_cap', 'index_build', 'index_value',
                    'index_indicators',
                    'v1_stock_indicator_state',
                    'v1_stock_indicator_enrichment',
                    -- V1 clean-pipeline Step 6 — industry index value populator.
                    'v1_industry_index_value'
                ));
    """)


def downgrade() -> None:
    # Remove any rows with the new phase value before restoring the
    # pre-0024 CHECK. fundamentals_run rows are transient audit records.
    op.execute("""
        DELETE FROM fundamentals_run
         WHERE phase = 'v1_industry_index_value';
    """)
    op.execute("""
        ALTER TABLE fundamentals_run
            DROP CONSTRAINT IF EXISTS fundamentals_run_phase_chk;
    """)
    op.execute("""
        ALTER TABLE fundamentals_run
            ADD CONSTRAINT fundamentals_run_phase_chk
                CHECK (phase IN (
                    'industry', 'shares', 'financials',
                    'market_cap', 'index_build', 'index_value',
                    'index_indicators',
                    'v1_stock_indicator_state',
                    'v1_stock_indicator_enrichment'
                ));
    """)
