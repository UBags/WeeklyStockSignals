---
-- Migration 016 — quarterly_financials time-series table (Phase 3).
--
-- Bit-equivalent parallel of alembic_pkg/versions/0009_quarterly_financials.py.
-- See that file's docstring for design rationale.
--
-- IMPORTANT — Phase 12 transition note:
--   This file is NOT applied to fresh databases. Alembic is the
--   migration system. The legacy parity test
--   (tests/persistence/test_legacy_migrator.py::test_legacy_and_alembic_schemas_match)
--   keeps this file column-equivalent to the Alembic revision.

BEGIN;

CREATE TABLE IF NOT EXISTS quarterly_financials (
    ticker_id       INTEGER     NOT NULL
                                REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    fiscal_quarter  DATE        NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revenue         NUMERIC(20, 2),
    ebitda          NUMERIC(20, 2),
    currency        VARCHAR(8)  NOT NULL DEFAULT 'INR',
    source          VARCHAR(24) NOT NULL DEFAULT 'YFINANCE',

    PRIMARY KEY (ticker_id, fiscal_quarter, fetched_at),

    CONSTRAINT quarterly_financials_source_chk
        CHECK (source IN ('YFINANCE', 'YFINANCE_COMPUTED',
                          'NSE_XBRL', 'MANUAL_OVERRIDE'))
);

CREATE INDEX IF NOT EXISTS idx_quarterly_financials_latest
    ON quarterly_financials (ticker_id, fiscal_quarter, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_quarterly_financials_quarter
    ON quarterly_financials (fiscal_quarter DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON quarterly_financials
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT ON quarterly_financials TO trip_gui_user;
    END IF;
END
$$;

-- Record this migration as applied (legacy migrator convention).
INSERT INTO schema_migrations (version) VALUES ('016') ON CONFLICT DO NOTHING;

COMMIT;
