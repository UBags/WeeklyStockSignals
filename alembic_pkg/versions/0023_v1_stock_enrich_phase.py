"""Migration 0023 — extend fundamentals_run.phase CHECK constraint.

Adds `'v1_stock_indicator_enrichment'` to the allowed values for
`fundamentals_run.phase`. Required by Step 5 of the clean-pipeline
V1 refactor — the new stock indicator enrichment populator opens
fundamentals_run rows with that phase value.

Same shape as migration 0022 — drop-then-recreate the CHECK with
the new value added. PostgreSQL has no ALTER for CHECK bodies.

Legacy parity: migrations/legacy/027_v1_stock_enrichment_phase.sql.

Revision ID: 0023_v1_stock_enrichment_phase
Revises:    0022_v1_stock_state_phase
Create Date: 2026-06-26
"""
from __future__ import annotations

from alembic import op


revision = "0023_v1_stock_enrich_phase"
down_revision = "0022_v1_stock_state_phase"
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
                    -- V1 clean-pipeline Step 5 — stock enrichment populator.
                    'v1_stock_indicator_enrichment'
                ));
    """)


def downgrade() -> None:
    # Remove any rows with the new phase value before restoring the
    # pre-0023 CHECK. fundamentals_run rows are transient audit records.
    op.execute("""
        DELETE FROM fundamentals_run
         WHERE phase = 'v1_stock_indicator_enrichment';
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
                    'v1_stock_indicator_state'
                ));
    """)
