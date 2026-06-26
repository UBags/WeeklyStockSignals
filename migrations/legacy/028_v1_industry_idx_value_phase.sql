---
-- Migration 028 — extend fundamentals_run.phase CHECK with
-- 'v1_industry_index_value' (Step 6 of clean-pipeline V1 refactor).
--
-- Bit-equivalent parallel of
-- alembic_pkg/versions/0024_v1_industry_idx_value_phase.py.
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
            'v1_stock_indicator_state',
            'v1_stock_indicator_enrichment',
            'v1_industry_index_value'
        ));

INSERT INTO schema_migrations (version) VALUES ('028') ON CONFLICT DO NOTHING;

COMMIT;
