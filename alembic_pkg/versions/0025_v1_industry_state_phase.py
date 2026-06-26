"""Migration 0025 — extend fundamentals_run.phase CHECK constraint.

Adds `'v1_industry_indicator_state'` to the allowed values for
`fundamentals_run.phase`. Required by Step 7 of the clean-pipeline
V1 refactor — the industry indicator state populator opens
fundamentals_run rows with that phase value.

Same shape as migrations 0022 / 0023 / 0024.

Legacy parity: migrations/legacy/029_v1_industry_state_phase.sql.

Revision ID: 0025_v1_industry_state_phase
Revises:    0024_v1_industry_idx_value_phase
Create Date: 2026-06-26
"""
from __future__ import annotations

from alembic import op


revision = "0025_v1_industry_state_phase"
down_revision = "0024_v1_industry_idx_value_phase"
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
                    'v1_industry_index_value',
                    -- V1 clean-pipeline Step 7 — industry indicator
                    -- state populator (weekly bars aggregated from
                    -- industry_index_value via daily_to_weekly).
                    'v1_industry_indicator_state'
                ));
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM fundamentals_run
         WHERE phase = 'v1_industry_indicator_state';
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
                    'v1_stock_indicator_enrichment',
                    'v1_industry_index_value'
                ));
    """)
