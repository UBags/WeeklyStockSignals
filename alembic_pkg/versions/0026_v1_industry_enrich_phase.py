"""Migration 0026 — extend fundamentals_run.phase CHECK constraint.

Adds `'v1_industry_indicator_enrichment'` to the allowed values for
`fundamentals_run.phase`. Required by Step 8 of the clean-pipeline
V1 refactor — the industry indicator enrichment populator opens
fundamentals_run rows with that phase value.

Same shape as migrations 0022 / 0023 / 0024 / 0025.

Legacy parity: migrations/legacy/030_v1_industry_enrich_phase.sql.

Revision ID: 0026_v1_industry_enrich_phase
Revises:    0025_v1_industry_state_phase
Create Date: 2026-06-26
"""
from __future__ import annotations

from alembic import op


revision = "0026_v1_industry_enrich_phase"
down_revision = "0025_v1_industry_state_phase"
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
                    'v1_industry_indicator_state',
                    -- V1 clean-pipeline Step 8 — industry indicator
                    -- enrichment populator (mirror of Step 5 stock
                    -- enrichment, runs on aggregated weekly bars).
                    -- Closes the V1 refactor.
                    'v1_industry_indicator_enrichment'
                ));
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM fundamentals_run
         WHERE phase = 'v1_industry_indicator_enrichment';
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
                    'v1_industry_index_value',
                    'v1_industry_indicator_state'
                ));
    """)
