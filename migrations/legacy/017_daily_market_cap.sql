---
-- Migration 017 — daily_market_cap time-series table (Phase 4A).
--
-- Bit-equivalent parallel of alembic_pkg/versions/0010_daily_market_cap.py.
-- See that file's docstring for design rationale.
--
-- IMPORTANT — Phase 12 transition note:
--   This file is NOT applied to fresh databases. Alembic is the
--   migration system. The legacy parity test
--   (tests/persistence/test_legacy_migrator.py::test_legacy_and_alembic_schemas_match)
--   keeps this file column-equivalent to the Alembic revision.

BEGIN;

CREATE TABLE IF NOT EXISTS daily_market_cap (
    ticker_id    INTEGER          NOT NULL
                                  REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    trade_date   DATE             NOT NULL,
    close_price  DOUBLE PRECISION NOT NULL,
    shares_used  BIGINT,
    market_cap   NUMERIC(22, 2)   NOT NULL,
    source       VARCHAR(24)      NOT NULL,
    computed_at  TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    PRIMARY KEY (ticker_id, trade_date),

    CONSTRAINT daily_market_cap_source_chk
        CHECK (source IN
               ('COMPUTED', 'YFINANCE_HIST_MCAP',
                'APPROX_TODAYS_SHARES', 'MANUAL_OVERRIDE')),
    CONSTRAINT daily_market_cap_positive
        CHECK (market_cap >= 0)
);

CREATE INDEX IF NOT EXISTS idx_daily_market_cap_date_desc
    ON daily_market_cap (trade_date, market_cap DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON daily_market_cap
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT ON daily_market_cap TO trip_gui_user;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES ('017') ON CONFLICT DO NOTHING;

COMMIT;
