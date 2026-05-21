---
-- Migration 015 — instrument_shares time-series table (Phase 2).
--
-- Bit-equivalent parallel of alembic_pkg/versions/0008_instrument_shares.py.
-- See that file's docstring for design rationale.
--
-- IMPORTANT — Phase 12 transition note:
--   This file is NOT applied to fresh databases. Alembic is the
--   migration system. The legacy parity test
--   (tests/persistence/test_legacy_migrator.py::test_legacy_and_alembic_schemas_match)
--   keeps this file column-equivalent to the Alembic revision.

BEGIN;

CREATE TABLE IF NOT EXISTS instrument_shares (
    ticker_id           INTEGER     NOT NULL
                                    REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    as_of_date          DATE        NOT NULL,
    shares_outstanding  BIGINT,
    free_float_factor   REAL,
    shares_source       VARCHAR(24) NOT NULL,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    xbrl_url            TEXT,

    PRIMARY KEY (ticker_id, as_of_date),

    CONSTRAINT instrument_shares_source_chk
        CHECK (shares_source IN
               ('YFINANCE', 'YFINANCE_IMPLIED',
                'NSE_XBRL', 'MANUAL_OVERRIDE'))
);

CREATE INDEX IF NOT EXISTS idx_instrument_shares_as_of
    ON instrument_shares (as_of_date DESC);
CREATE INDEX IF NOT EXISTS idx_instrument_shares_ticker_desc
    ON instrument_shares (ticker_id, as_of_date DESC);

-- Grants (idempotent).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON instrument_shares
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT ON instrument_shares TO trip_gui_user;
    END IF;
END
$$;

-- Record this migration as applied (legacy migrator convention).
INSERT INTO schema_migrations (version) VALUES ('015') ON CONFLICT DO NOTHING;

COMMIT;
