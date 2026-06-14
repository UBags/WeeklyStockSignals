---
-- Migration 020 — industry_index_value table (Phase 4B-1).
--
-- Bit-equivalent parallel of
-- alembic_pkg/versions/0013_industry_index_value.py.

BEGIN;

CREATE TABLE IF NOT EXISTS industry_index_value (
    industry_name             VARCHAR(128)     NOT NULL
                                               REFERENCES industry_index(industry_name) ON DELETE CASCADE,
    trade_date                DATE             NOT NULL,
    value                     DOUBLE PRECISION NOT NULL,
    constituents_used         INTEGER          NOT NULL,
    constituents_missing      INTEGER          NOT NULL DEFAULT 0,
    renormalized              BOOLEAN          NOT NULL DEFAULT FALSE,
    effective_rebalance_date  DATE             NOT NULL,
    computed_at               TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    PRIMARY KEY (industry_name, trade_date),

    CONSTRAINT iiv_value_chk CHECK (value > 0),
    CONSTRAINT iiv_used_chk CHECK (constituents_used >= 0),
    CONSTRAINT iiv_missing_chk CHECK (constituents_missing >= 0)
);

CREATE INDEX IF NOT EXISTS idx_industry_index_value_date
    ON industry_index_value (trade_date DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON industry_index_value
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT ON industry_index_value TO trip_gui_user;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES ('020') ON CONFLICT DO NOTHING;

COMMIT;
