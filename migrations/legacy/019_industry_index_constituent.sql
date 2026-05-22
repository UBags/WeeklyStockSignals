---
-- Migration 019 — industry_index_constituent table (Phase 4B-1).
--
-- Bit-equivalent parallel of
-- alembic_pkg/versions/0012_industry_index_constituent.py.

BEGIN;

CREATE TABLE IF NOT EXISTS industry_index_constituent (
    industry_name           VARCHAR(128)     NOT NULL
                                            REFERENCES industry_index(industry_name) ON DELETE CASCADE,
    ticker_id               INTEGER          NOT NULL
                                            REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    rebalance_date          DATE             NOT NULL,
    effective_from          DATE             NOT NULL,
    effective_to            DATE,
    weight_at_rebalance     DOUBLE PRECISION NOT NULL,
    market_cap_at_rebalance NUMERIC(22, 2)   NOT NULL,
    close_at_rebalance      DOUBLE PRECISION NOT NULL,
    removed_reason          VARCHAR(32),

    PRIMARY KEY (industry_name, ticker_id, effective_from),

    CONSTRAINT iic_weight_chk
        CHECK (weight_at_rebalance >= 0 AND weight_at_rebalance <= 1),
    CONSTRAINT iic_mcap_chk
        CHECK (market_cap_at_rebalance > 0),
    CONSTRAINT iic_close_chk
        CHECK (close_at_rebalance > 0),
    CONSTRAINT iic_range_chk
        CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CONSTRAINT iic_removed_reason_chk
        CHECK (removed_reason IS NULL OR removed_reason IN
               ('REBALANCE_EXIT', 'DELISTED', 'OPERATOR_REMOVED'))
);

CREATE INDEX IF NOT EXISTS idx_iic_active
    ON industry_index_constituent (industry_name, ticker_id)
    WHERE effective_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_iic_asof
    ON industry_index_constituent (industry_name, effective_from DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON industry_index_constituent
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT ON industry_index_constituent TO trip_gui_user;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES ('019') ON CONFLICT DO NOTHING;

COMMIT;
