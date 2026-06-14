---
-- Migration 013 — fundamentals Phase 1: instrument_industry,
-- industry_override, fundamentals_run.
--
-- Bit-equivalent parallel of alembic_pkg/versions/0006_fundamentals_phase1.py.
-- See that file's docstring for design rationale.
--
-- IMPORTANT — Phase 12 transition note:
--   This file is NOT applied to fresh databases. Alembic is the
--   migration system. The legacy parity test
--   (tests/persistence/test_legacy_migrator.py::test_legacy_and_alembic_schemas_match)
--   keeps this file column-equivalent to the Alembic revision.

BEGIN;

CREATE TABLE IF NOT EXISTS instrument_industry (
    ticker_id              INTEGER     PRIMARY KEY
                                       REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    sector                 VARCHAR(64),
    industry               VARCHAR(128),
    classification_source  VARCHAR(32) NOT NULL DEFAULT 'YFINANCE',
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT instrument_industry_source_chk
        CHECK (classification_source IN
               ('YFINANCE', 'GROWW', 'MANUAL_OVERRIDE', 'NSE_SECTORAL'))
);

CREATE INDEX IF NOT EXISTS idx_instrument_industry_industry
    ON instrument_industry (industry);
CREATE INDEX IF NOT EXISTS idx_instrument_industry_sector
    ON instrument_industry (sector);

CREATE TABLE IF NOT EXISTS industry_override (
    ticker_id    INTEGER     PRIMARY KEY
                             REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    sector       VARCHAR(64)  NOT NULL,
    industry     VARCHAR(128) NOT NULL,
    note         TEXT,
    set_by       VARCHAR(64),
    set_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fundamentals_run (
    run_id        BIGSERIAL    PRIMARY KEY,
    phase         VARCHAR(32)  NOT NULL,
    exchange      VARCHAR(16),
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    status        VARCHAR(16)  NOT NULL DEFAULT 'RUNNING',
    succeeded     INTEGER      NOT NULL DEFAULT 0,
    failed        INTEGER      NOT NULL DEFAULT 0,
    skipped       INTEGER      NOT NULL DEFAULT 0,
    progress_json JSONB,
    notes         TEXT,

    CONSTRAINT fundamentals_run_phase_chk
        CHECK (phase IN ('industry', 'shares', 'financials',
                         'market_cap', 'index_build', 'index_value',
                         'index_indicators')),
    CONSTRAINT fundamentals_run_status_chk
        CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS idx_fundamentals_run_phase_ex
    ON fundamentals_run (phase, exchange, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_fundamentals_run_completed
    ON fundamentals_run (phase, exchange, completed_at DESC)
    WHERE status = 'SUCCESS';

-- Grants (idempotent).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON instrument_industry, industry_override, fundamentals_run
            TO trip_etl_user;
        GRANT USAGE, SELECT ON SEQUENCE fundamentals_run_run_id_seq
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT
            ON instrument_industry, industry_override, fundamentals_run
            TO trip_gui_user;
    END IF;
END
$$;


-- Record this migration as applied (legacy migrator convention).
INSERT INTO schema_migrations (version) VALUES ('013') ON CONFLICT DO NOTHING;

COMMIT;
