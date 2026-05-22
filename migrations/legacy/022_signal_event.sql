---
-- Migration 022 — signal_event table (Phase 6-1).
--
-- Bit-equivalent parallel of alembic_pkg/versions/0015_signal_event.py.

BEGIN;

CREATE TABLE IF NOT EXISTS signal_event (
    ticker_id        INTEGER          REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    timeframe        VARCHAR(8),
    industry_name    VARCHAR(128)     REFERENCES industry_index(industry_name) ON DELETE CASCADE,

    trade_date       DATE             NOT NULL,
    event_kind       VARCHAR(16)      NOT NULL,
    regime_before    SMALLINT         NOT NULL,
    regime_after     SMALLINT         NOT NULL,
    close_at_event   DOUBLE PRECISION NOT NULL,
    computed_at      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

    CONSTRAINT signal_event_series_xor_chk
        CHECK (
            (ticker_id IS NOT NULL AND timeframe IS NOT NULL AND industry_name IS NULL)
            OR
            (ticker_id IS NULL AND timeframe IS NULL AND industry_name IS NOT NULL)
        ),
    CONSTRAINT signal_event_kind_chk
        CHECK (event_kind IN ('BUY', 'SELL', 'EXIT_BUY', 'EXIT_SELL')),
    CONSTRAINT signal_event_regime_before_chk
        CHECK (regime_before IN (-1, 0, 1)),
    CONSTRAINT signal_event_regime_after_chk
        CHECK (regime_after IN (-1, 0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_event_ticker
    ON signal_event (ticker_id, timeframe, trade_date, event_kind)
    WHERE ticker_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_event_industry
    ON signal_event (industry_name, trade_date, event_kind)
    WHERE industry_name IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_signal_event_trade_date
    ON signal_event (trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_signal_event_kind_date
    ON signal_event (event_kind, trade_date DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON signal_event
            TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT ON signal_event TO trip_gui_user;
    END IF;
END
$$;

INSERT INTO schema_migrations (version) VALUES ('022') ON CONFLICT DO NOTHING;

COMMIT;
