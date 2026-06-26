---
-- Migration 025 — indicator enrichment tables (Step 2 of clean
-- pipeline V1 refactor).
--
-- Bit-equivalent parallel of
-- alembic_pkg/versions/0021_indicator_enrichment_tables.py.
-- See that file's docstring for the design.
--
-- Tables added:
--   stock_indicator_enrichment    — 1:1 with stock_indicator_state
--   industry_indicator_enrichment — 1:1 with industry_indicator_state
--
-- Each enrichment row is keyed by the same composite PK as its
-- matching state row, with ON DELETE CASCADE so dropping a state row
-- drops the enrichment row with it. No additional indexes — the PK
-- (== FK columns) serves the JOIN access pattern.
---

BEGIN;

-- ====================================================================
-- stock_indicator_enrichment
-- ====================================================================
CREATE TABLE IF NOT EXISTS stock_indicator_enrichment (
    ticker_id          INTEGER          NOT NULL,
    timeframe          VARCHAR(8)       NOT NULL,
    bar_timestamp      TIMESTAMPTZ      NOT NULL,

    pierces_upper_mid  BOOLEAN          NOT NULL DEFAULT FALSE,
    pierces_lower_mid  BOOLEAN          NOT NULL DEFAULT FALSE,

    dist_to_med_in_hl_units            DOUBLE PRECISION,
    dist_to_long_in_hl_units           DOUBLE PRECISION,
    dist_to_longest_in_hl_units        DOUBLE PRECISION,

    mean_dist_to_med_in_hl_units       DOUBLE PRECISION,
    mean_dist_to_long_in_hl_units      DOUBLE PRECISION,
    mean_dist_to_longest_in_hl_units   DOUBLE PRECISION,

    bars_since_med_turn                INTEGER,
    bars_since_long_turn               INTEGER,
    bars_since_longest_turn            INTEGER,

    pct_change_since_med_turn          DOUBLE PRECISION,
    pct_change_since_long_turn         DOUBLE PRECISION,
    pct_change_since_longest_turn      DOUBLE PRECISION,

    med_pattern_label      VARCHAR(32)  NOT NULL DEFAULT 'NA',
    long_pattern_label     VARCHAR(32)  NOT NULL DEFAULT 'NA',
    longest_pattern_label  VARCHAR(32)  NOT NULL DEFAULT 'NA',

    med_turn_buy           BOOLEAN      NOT NULL DEFAULT FALSE,
    med_turn_sell          BOOLEAN      NOT NULL DEFAULT FALSE,
    long_turn_buy          BOOLEAN      NOT NULL DEFAULT FALSE,
    long_turn_sell         BOOLEAN      NOT NULL DEFAULT FALSE,
    longest_turn_buy       BOOLEAN      NOT NULL DEFAULT FALSE,
    longest_turn_sell      BOOLEAN      NOT NULL DEFAULT FALSE,

    computed_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    PRIMARY KEY (ticker_id, timeframe, bar_timestamp),

    CONSTRAINT stock_indicator_enrichment_state_fk
        FOREIGN KEY (ticker_id, timeframe, bar_timestamp)
        REFERENCES stock_indicator_state
            (ticker_id, timeframe, bar_timestamp)
        ON DELETE CASCADE
);

COMMENT ON TABLE stock_indicator_enrichment IS
    'V1 per-ticker enrichment fields (band-pierce flags, distance '
    'metrics, bars-since-turn, pct-change-since-turn, pattern labels, '
    'per-EMA turn flags). 1:1 with stock_indicator_state via composite '
    'FK with ON DELETE CASCADE. Separated from core so the enrichment '
    'set can evolve without touching the stable core schema.';

-- ====================================================================
-- industry_indicator_enrichment
-- ====================================================================
CREATE TABLE IF NOT EXISTS industry_indicator_enrichment (
    industry_name      VARCHAR(128)     NOT NULL,
    timeframe          VARCHAR(8)       NOT NULL,
    bar_timestamp      TIMESTAMPTZ      NOT NULL,

    pierces_upper_mid  BOOLEAN          NOT NULL DEFAULT FALSE,
    pierces_lower_mid  BOOLEAN          NOT NULL DEFAULT FALSE,

    dist_to_med_in_hl_units            DOUBLE PRECISION,
    dist_to_long_in_hl_units           DOUBLE PRECISION,
    dist_to_longest_in_hl_units        DOUBLE PRECISION,

    mean_dist_to_med_in_hl_units       DOUBLE PRECISION,
    mean_dist_to_long_in_hl_units      DOUBLE PRECISION,
    mean_dist_to_longest_in_hl_units   DOUBLE PRECISION,

    bars_since_med_turn                INTEGER,
    bars_since_long_turn               INTEGER,
    bars_since_longest_turn            INTEGER,

    pct_change_since_med_turn          DOUBLE PRECISION,
    pct_change_since_long_turn         DOUBLE PRECISION,
    pct_change_since_longest_turn      DOUBLE PRECISION,

    med_pattern_label      VARCHAR(32)  NOT NULL DEFAULT 'NA',
    long_pattern_label     VARCHAR(32)  NOT NULL DEFAULT 'NA',
    longest_pattern_label  VARCHAR(32)  NOT NULL DEFAULT 'NA',

    med_turn_buy           BOOLEAN      NOT NULL DEFAULT FALSE,
    med_turn_sell          BOOLEAN      NOT NULL DEFAULT FALSE,
    long_turn_buy          BOOLEAN      NOT NULL DEFAULT FALSE,
    long_turn_sell         BOOLEAN      NOT NULL DEFAULT FALSE,
    longest_turn_buy       BOOLEAN      NOT NULL DEFAULT FALSE,
    longest_turn_sell      BOOLEAN      NOT NULL DEFAULT FALSE,

    computed_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    PRIMARY KEY (industry_name, timeframe, bar_timestamp),

    CONSTRAINT industry_indicator_enrichment_state_fk
        FOREIGN KEY (industry_name, timeframe, bar_timestamp)
        REFERENCES industry_indicator_state
            (industry_name, timeframe, bar_timestamp)
        ON DELETE CASCADE
);

COMMENT ON TABLE industry_indicator_enrichment IS
    'V1 per-industry enrichment fields. Mirror of '
    'stock_indicator_enrichment. 1:1 with industry_indicator_state '
    'via composite FK with ON DELETE CASCADE.';

-- ====================================================================
-- Grants
-- ====================================================================
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON stock_indicator_enrichment, industry_indicator_enrichment
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT
            ON stock_indicator_enrichment, industry_indicator_enrichment
            TO trip_gui_user;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES ('025') ON CONFLICT DO NOTHING;

COMMIT;
