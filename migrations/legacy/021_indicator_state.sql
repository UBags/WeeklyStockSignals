---
-- Migration 021 — indicator_state table (Phase 5-1).
--
-- Bit-equivalent parallel of alembic_pkg/versions/0014_indicator_state.py.
-- See that file's docstring for the design.

BEGIN;

CREATE TABLE IF NOT EXISTS indicator_state (
    ticker_id        INTEGER          REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    timeframe        VARCHAR(8),
    industry_name    VARCHAR(128)     REFERENCES industry_index(industry_name) ON DELETE CASCADE,
    trade_date       DATE             NOT NULL,

    med_ema          DOUBLE PRECISION NOT NULL,
    long_ema         DOUBLE PRECISION NOT NULL,
    longest_ema      DOUBLE PRECISION NOT NULL,

    atr              DOUBLE PRECISION NOT NULL,
    upper_inner_band DOUBLE PRECISION NOT NULL,
    lower_inner_band DOUBLE PRECISION NOT NULL,
    upper_mid_band   DOUBLE PRECISION NOT NULL,
    lower_mid_band   DOUBLE PRECISION NOT NULL,
    upper_outer_band DOUBLE PRECISION NOT NULL,
    lower_outer_band DOUBLE PRECISION NOT NULL,

    core_regime      SMALLINT         NOT NULL,
    display_regime   SMALLINT         NOT NULL,

    pierces_upper_mid BOOLEAN         NOT NULL DEFAULT FALSE,
    pierces_lower_mid BOOLEAN         NOT NULL DEFAULT FALSE,

    dist_to_med_in_hl_units      DOUBLE PRECISION,
    dist_to_long_in_hl_units     DOUBLE PRECISION,
    dist_to_longest_in_hl_units  DOUBLE PRECISION,
    mean_dist_to_med_in_hl_units      DOUBLE PRECISION,
    mean_dist_to_long_in_hl_units     DOUBLE PRECISION,
    mean_dist_to_longest_in_hl_units  DOUBLE PRECISION,

    bars_since_med_turn      INTEGER,
    bars_since_long_turn     INTEGER,
    bars_since_longest_turn  INTEGER,
    pct_change_since_med_turn      DOUBLE PRECISION,
    pct_change_since_long_turn     DOUBLE PRECISION,
    pct_change_since_longest_turn  DOUBLE PRECISION,

    med_pattern_label      VARCHAR(32) NOT NULL DEFAULT 'NA',
    long_pattern_label     VARCHAR(32) NOT NULL DEFAULT 'NA',
    longest_pattern_label  VARCHAR(32) NOT NULL DEFAULT 'NA',

    med_turn_buy        BOOLEAN NOT NULL DEFAULT FALSE,
    med_turn_sell       BOOLEAN NOT NULL DEFAULT FALSE,
    long_turn_buy       BOOLEAN NOT NULL DEFAULT FALSE,
    long_turn_sell      BOOLEAN NOT NULL DEFAULT FALSE,
    longest_turn_buy    BOOLEAN NOT NULL DEFAULT FALSE,
    longest_turn_sell   BOOLEAN NOT NULL DEFAULT FALSE,

    computed_at      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    CONSTRAINT indicator_state_series_xor_chk
        CHECK (
            (ticker_id IS NOT NULL AND timeframe IS NOT NULL AND industry_name IS NULL)
            OR
            (ticker_id IS NULL AND timeframe IS NULL AND industry_name IS NOT NULL)
        ),
    CONSTRAINT indicator_state_core_regime_chk
        CHECK (core_regime IN (-1, 0, 1)),
    CONSTRAINT indicator_state_display_regime_chk
        CHECK (display_regime IN (-1, 0, 1)),
    CONSTRAINT indicator_state_atr_chk
        CHECK (atr >= 0),
    CONSTRAINT indicator_state_emas_positive_chk
        CHECK (med_ema > 0 AND long_ema > 0 AND longest_ema > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_indicator_state_ticker
    ON indicator_state (ticker_id, timeframe, trade_date)
    WHERE ticker_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_indicator_state_industry
    ON indicator_state (industry_name, trade_date)
    WHERE industry_name IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_indicator_state_trade_date
    ON indicator_state (trade_date DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON indicator_state
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT ON indicator_state TO trip_gui_user;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES ('021') ON CONFLICT DO NOTHING;

COMMIT;
