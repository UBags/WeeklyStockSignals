---
-- Migration 026 — extend fundamentals_run.phase CHECK with
-- 'v1_stock_indicator_state' (Step 4 of clean-pipeline V1 refactor).
--
-- Bit-equivalent parallel of
-- alembic_pkg/versions/0022_v1_stock_state_phase.py. See that file's
-- docstring for the design.
---

BEGIN;

ALTER TABLE fundamentals_run
    DROP CONSTRAINT IF EXISTS fundamentals_run_phase_chk;

ALTER TABLE fundamentals_run
    ADD CONSTRAINT fundamentals_run_phase_chk
        CHECK (phase IN (
            'industry', 'shares', 'financials',
            'market_cap', 'index_build', 'index_value',
            'index_indicators',
            -- V1 clean-pipeline phases.
            'v1_stock_indicator_state'
        ));

INSERT INTO schema_migrations (version) VALUES ('026') ON CONFLICT DO NOTHING;

COMMIT;
