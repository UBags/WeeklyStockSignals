---
-- Migration 030 — extend fundamentals_run.phase CHECK with
-- 'v1_industry_indicator_enrichment' (Step 8 of clean-pipeline V1
-- refactor — closes the V1 refactor).
--
-- Bit-equivalent parallel of
-- alembic_pkg/versions/0026_v1_industry_enrich_phase.py.
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
            'v1_industry_index_value',
            'v1_industry_indicator_state',
            'v1_industry_indicator_enrichment'
        ));

INSERT INTO schema_migrations (version) VALUES ('030') ON CONFLICT DO NOTHING;

COMMIT;
