---
-- Migration 024 — clean pipeline V1 tables (Step 1 of CLEAN_PIPELINE_REFACTOR).
--
-- Bit-equivalent parallel of alembic_pkg/versions/0020_clean_pipeline_tables.py.
-- See that file's docstring for design rationale.
--
-- IMPORTANT — Phase 12 transition note:
--   This file is NOT applied to fresh databases. Alembic is the
--   migration system. The legacy parity test
--   (tests/persistence/test_legacy_migrator.py::test_alembic_and_legacy_produce_same_tables)
--   keeps this file column-equivalent to the Alembic revision.

BEGIN;

-- ====================================================
-- stock_indicator_state
-- ====================================================
CREATE TABLE IF NOT EXISTS stock_indicator_state (
    ticker_id          INTEGER          NOT NULL
                                        REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    timeframe          VARCHAR(8)       NOT NULL,
    bar_timestamp      TIMESTAMPTZ      NOT NULL,

    med_ema            DOUBLE PRECISION NOT NULL,
    long_ema           DOUBLE PRECISION NOT NULL,
    longest_ema        DOUBLE PRECISION NOT NULL,

    atr                DOUBLE PRECISION NOT NULL,

    upper_inner_band   DOUBLE PRECISION NOT NULL,
    lower_inner_band   DOUBLE PRECISION NOT NULL,
    upper_mid_band     DOUBLE PRECISION NOT NULL,
    lower_mid_band     DOUBLE PRECISION NOT NULL,
    upper_outer_band   DOUBLE PRECISION NOT NULL,
    lower_outer_band   DOUBLE PRECISION NOT NULL,

    display_regime     SMALLINT         NOT NULL,

    computed_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    PRIMARY KEY (ticker_id, timeframe, bar_timestamp),

    CONSTRAINT stock_indicator_state_display_regime_chk
        CHECK (display_regime IN (-1, 0, 1)),
    CONSTRAINT stock_indicator_state_atr_chk
        CHECK (atr >= 0),
    CONSTRAINT stock_indicator_state_emas_positive_chk
        CHECK (med_ema > 0 AND long_ema > 0 AND longest_ema > 0)
);

CREATE INDEX IF NOT EXISTS idx_stock_indicator_state_bar_ts
    ON stock_indicator_state (bar_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_stock_indicator_state_ticker_bar
    ON stock_indicator_state (ticker_id, bar_timestamp DESC);

COMMENT ON TABLE stock_indicator_state IS
    'V1 per-ticker EMA/ATR/regime trajectory. PK (ticker_id, '
    'timeframe, bar_timestamp). Replaces ticker rows of the '
    'polymorphic indicator_state. Upserted incrementally each '
    'cycle (last N weeks) or fully via Step 17 regenerate CLI.';

-- ====================================================
-- industry_indicator_state
-- ====================================================
CREATE TABLE IF NOT EXISTS industry_indicator_state (
    industry_name      VARCHAR(128)     NOT NULL
                                        REFERENCES industry_index(industry_name) ON DELETE CASCADE,
    timeframe          VARCHAR(8)       NOT NULL,
    bar_timestamp      TIMESTAMPTZ      NOT NULL,

    med_ema            DOUBLE PRECISION NOT NULL,
    long_ema           DOUBLE PRECISION NOT NULL,
    longest_ema        DOUBLE PRECISION NOT NULL,

    atr                DOUBLE PRECISION NOT NULL,

    upper_inner_band   DOUBLE PRECISION NOT NULL,
    lower_inner_band   DOUBLE PRECISION NOT NULL,
    upper_mid_band     DOUBLE PRECISION NOT NULL,
    lower_mid_band     DOUBLE PRECISION NOT NULL,
    upper_outer_band   DOUBLE PRECISION NOT NULL,
    lower_outer_band   DOUBLE PRECISION NOT NULL,

    display_regime     SMALLINT         NOT NULL,

    computed_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    PRIMARY KEY (industry_name, timeframe, bar_timestamp),

    CONSTRAINT industry_indicator_state_display_regime_chk
        CHECK (display_regime IN (-1, 0, 1)),
    CONSTRAINT industry_indicator_state_atr_chk
        CHECK (atr >= 0),
    CONSTRAINT industry_indicator_state_emas_positive_chk
        CHECK (med_ema > 0 AND long_ema > 0 AND longest_ema > 0)
);

CREATE INDEX IF NOT EXISTS idx_industry_indicator_state_bar_ts
    ON industry_indicator_state (bar_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_industry_indicator_state_industry_bar
    ON industry_indicator_state (industry_name, bar_timestamp DESC);

COMMENT ON TABLE industry_indicator_state IS
    'V1 per-industry EMA/ATR/regime trajectory. PK '
    '(industry_name, timeframe, bar_timestamp). Replaces '
    'industry rows of the polymorphic indicator_state. '
    'Synthetic ATR semantics (Phase 1B).';

-- ====================================================
-- current_signals
-- ====================================================
CREATE TABLE IF NOT EXISTS current_signals (
    ticker_id          INTEGER          PRIMARY KEY
                                        REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    timeframe          VARCHAR(8)       NOT NULL,
    bar_timestamp      TIMESTAMPTZ      NOT NULL,
    signal_type        VARCHAR(16)      NOT NULL,
    regime_before      SMALLINT         NOT NULL,
    regime_after       SMALLINT         NOT NULL,
    price_at_signal    DOUBLE PRECISION NOT NULL,
    computed_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    CONSTRAINT current_signals_signal_type_chk
        CHECK (signal_type IN ('BUY', 'SELL', 'EXIT_FROM_BUY', 'EXIT_FROM_SELL')),
    CONSTRAINT current_signals_regime_before_chk
        CHECK (regime_before IN (-1, 0, 1)),
    CONSTRAINT current_signals_regime_after_chk
        CHECK (regime_after IN (-1, 0, 1)),
    CONSTRAINT current_signals_regime_changes_chk
        CHECK (regime_before <> regime_after),
    CONSTRAINT current_signals_price_chk
        CHECK (price_at_signal > 0)
);

COMMENT ON TABLE current_signals IS
    'V1 signals fired in the CURRENT cycle only. One row per '
    'ticker that just changed regime. PK ticker_id (max one '
    'row per ticker). Rotated wholesale into historical_signals '
    'at start of next cycle. Typical size < 100 rows.';

-- ====================================================
-- historical_signals
-- ====================================================
CREATE TABLE IF NOT EXISTS historical_signals (
    signal_id          BIGSERIAL        PRIMARY KEY,
    ticker_id          INTEGER          NOT NULL
                                        REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    timeframe          VARCHAR(8)       NOT NULL,
    bar_timestamp      TIMESTAMPTZ      NOT NULL,
    signal_type        VARCHAR(16)      NOT NULL,
    regime_before      SMALLINT         NOT NULL,
    regime_after       SMALLINT         NOT NULL,
    price_at_signal    DOUBLE PRECISION NOT NULL,
    computed_at        TIMESTAMPTZ      NOT NULL,
    archived_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    CONSTRAINT historical_signals_signal_type_chk
        CHECK (signal_type IN ('BUY', 'SELL', 'EXIT_FROM_BUY', 'EXIT_FROM_SELL')),
    CONSTRAINT historical_signals_regime_before_chk
        CHECK (regime_before IN (-1, 0, 1)),
    CONSTRAINT historical_signals_regime_after_chk
        CHECK (regime_after IN (-1, 0, 1)),
    CONSTRAINT historical_signals_regime_changes_chk
        CHECK (regime_before <> regime_after),
    CONSTRAINT historical_signals_price_chk
        CHECK (price_at_signal > 0)
);

CREATE INDEX IF NOT EXISTS idx_historical_signals_ticker_bar
    ON historical_signals (ticker_id, bar_timestamp DESC, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_historical_signals_bar_ts
    ON historical_signals (bar_timestamp DESC, computed_at DESC);

COMMENT ON TABLE historical_signals IS
    'V1 append-only audit log of every signal ever fired. '
    'No uniqueness beyond signal_id BIGSERIAL — rotation '
    'across cycles intentionally produces duplicate rows '
    'while the source bar remains the latest. Read-side '
    'DISTINCT ON (bar_timestamp, signal_type) dedupes for '
    'GUI display.';

-- ====================================================
-- Grants
-- ====================================================
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON stock_indicator_state,
               industry_indicator_state,
               current_signals,
               historical_signals
            TO trip_etl_user;
        GRANT USAGE, SELECT
            ON SEQUENCE historical_signals_signal_id_seq
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT
            ON stock_indicator_state,
               industry_indicator_state,
               current_signals,
               historical_signals
            TO trip_gui_user;
    END IF;
END
$$;

-- Record this migration as applied (legacy migrator convention).
INSERT INTO schema_migrations (version) VALUES ('024') ON CONFLICT DO NOTHING;

COMMIT;
