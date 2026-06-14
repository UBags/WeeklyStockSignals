---
-- Migration 018 — industry_index table (Phase 4B-1).
--
-- Bit-equivalent parallel of alembic_pkg/versions/0011_industry_index.py.
-- See that file's docstring for design rationale.

BEGIN;

CREATE TABLE IF NOT EXISTS industry_index (
    industry_name        VARCHAR(128)     NOT NULL,
    sector_name          VARCHAR(128)     NOT NULL,
    methodology          VARCHAR(32)      NOT NULL DEFAULT 'MODIFIED_LASPEYRES',
    base_value           DOUBLE PRECISION NOT NULL DEFAULT 1000.0,
    base_date            DATE             NOT NULL,
    rebalance_schedule   VARCHAR(32)      NOT NULL DEFAULT 'SEMIANNUAL_APR_OCT',
    min_constituents     INTEGER          NOT NULL DEFAULT 10,
    status               VARCHAR(16)      NOT NULL DEFAULT 'ACTIVE',
    notes                TEXT,

    PRIMARY KEY (industry_name),

    CONSTRAINT industry_index_methodology_chk
        CHECK (methodology IN ('MODIFIED_LASPEYRES',
                               'EQUAL_WEIGHTED',
                               'FREE_FLOAT_WEIGHTED')),
    CONSTRAINT industry_index_schedule_chk
        CHECK (rebalance_schedule IN ('SEMIANNUAL_APR_OCT',
                                      'QUARTERLY',
                                      'ANNUAL_APR',
                                      'MANUAL')),
    CONSTRAINT industry_index_status_chk
        CHECK (status IN ('ACTIVE', 'RETIRED')),
    CONSTRAINT industry_index_base_value_chk
        CHECK (base_value > 0),
    CONSTRAINT industry_index_min_constituents_chk
        CHECK (min_constituents >= 1)
);

CREATE INDEX IF NOT EXISTS idx_industry_index_active
    ON industry_index (status, industry_name)
    WHERE status = 'ACTIVE';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON industry_index
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT ON industry_index TO trip_gui_user;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES ('018') ON CONFLICT DO NOTHING;

COMMIT;
