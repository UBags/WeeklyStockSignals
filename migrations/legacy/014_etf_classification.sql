---
-- Migration 014 — ETF classification: 'ETF' source + etf_long_name column.
--
-- Bit-equivalent parallel of alembic_pkg/versions/0007_etf_classification.py.
-- See that file's docstring for rationale.

BEGIN;

-- 1. Allow 'INSTRUMENT_TYPE' as a classification_source value.
ALTER TABLE instrument_industry
    DROP CONSTRAINT IF EXISTS instrument_industry_source_chk;
ALTER TABLE instrument_industry
    ADD CONSTRAINT instrument_industry_source_chk
    CHECK (classification_source IN (
        'YFINANCE', 'GROWW', 'MANUAL_OVERRIDE',
        'NSE_SECTORAL', 'INSTRUMENT_TYPE'
    ));

-- 2. ETF long-name column (NULL for equities).
ALTER TABLE instrument_industry
    ADD COLUMN IF NOT EXISTS etf_long_name VARCHAR(256);

-- Record this migration as applied (legacy migrator convention).
INSERT INTO schema_migrations (version) VALUES ('014') ON CONFLICT DO NOTHING;

COMMIT;
