"""Migration 0022 — extend fundamentals_run.phase CHECK constraint.

Adds `'v1_stock_indicator_state'` to the allowed values for
`fundamentals_run.phase`. Required by Step 4 of the clean-pipeline
V1 refactor — the new stock indicator state populator opens
fundamentals_run rows with that phase value.

The phase CHECK is defined at the DB level (CHECK constraint
fundamentals_run_phase_chk in migration 0006_fundamentals_phase1)
and at the application level (RUN_PHASES tuple in
trip_ema/fundamentals/__init__.py). The Python tuple has already
been extended; this migration keeps the DB in sync.

Forward-looking: Steps 5, 7, 8 will add three more V1 phases
(v1_stock_indicator_enrichment, v1_industry_indicator_state,
v1_industry_indicator_enrichment). Each gets its own migration
that follows this same pattern — drop the CHECK, recreate with the
new value included.

Legacy parity: migrations/legacy/026_v1_stock_state_phase.sql.

Revision ID: 0022_v1_stock_state_phase
Revises:    0021_indicator_enrichment
Create Date: 2026-06-26
"""
from __future__ import annotations

from alembic import op


revision = "0022_v1_stock_state_phase"
down_revision = "0021_indicator_enrichment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop-then-recreate is the standard pattern for "extend a CHECK
    # IN (...) constraint" — PostgreSQL has no ALTER for CHECK bodies.
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
                    -- V1 clean-pipeline phases.
                    'v1_stock_indicator_state'
                ));
    """)


def downgrade() -> None:
    # The downgrade restores the pre-0022 phase set. Any existing rows
    # already in fundamentals_run with phase='v1_stock_indicator_state'
    # would violate the restored CHECK — so we delete them first to
    # keep the downgrade idempotent and lossy-but-safe. The Step 4
    # runner is the only producer; rows are transient run-audit
    # records, not user data.
    op.execute("""
        DELETE FROM fundamentals_run
         WHERE phase = 'v1_stock_indicator_state';
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
                    'index_indicators'
                ));
    """)
