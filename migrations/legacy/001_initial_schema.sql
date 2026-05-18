-- 001_initial_schema.sql
-- The foundation: tickers and their OHLCV bars.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR(16) PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tickers (
    ticker_id   SERIAL PRIMARY KEY,
    exchange    VARCHAR(16)  NOT NULL,
    symbol      VARCHAR(32)  NOT NULL,
    instrument  VARCHAR(16)  NOT NULL DEFAULT 'EQ',
    full_name   VARCHAR(128),
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (exchange, symbol, instrument)
);

CREATE INDEX IF NOT EXISTS idx_tickers_active ON tickers (is_active) WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS ohlcv_bars (
    ticker_id      INTEGER          NOT NULL REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    timeframe      VARCHAR(8)       NOT NULL,
    bar_timestamp  TIMESTAMPTZ      NOT NULL,
    open           DOUBLE PRECISION NOT NULL,
    high           DOUBLE PRECISION NOT NULL,
    low            DOUBLE PRECISION NOT NULL,
    close          DOUBLE PRECISION NOT NULL,
    volume         DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (ticker_id, timeframe, bar_timestamp)
);

-- Range scans over the last N bars for a ticker are the hottest read path.
CREATE INDEX IF NOT EXISTS idx_bars_lookup
    ON ohlcv_bars (ticker_id, timeframe, bar_timestamp DESC);

INSERT INTO schema_migrations (version) VALUES ('001') ON CONFLICT DO NOTHING;
