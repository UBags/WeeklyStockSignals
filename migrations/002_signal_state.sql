-- 002_signal_state.sql
-- Per-ticker live state cache and append-only event log.

CREATE TABLE IF NOT EXISTS signal_events (
    event_id         BIGSERIAL    PRIMARY KEY,
    ticker_id        INTEGER      NOT NULL REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    timeframe        VARCHAR(8)   NOT NULL,
    bar_timestamp    TIMESTAMPTZ  NOT NULL,
    signal_type      VARCHAR(16)  NOT NULL
        CHECK (signal_type IN ('BUY', 'SELL', 'EXIT_FROM_BUY', 'EXIT_FROM_SELL')),
    price_at_signal  DOUBLE PRECISION NOT NULL,
    computed_at      TIMESTAMPTZ  NOT NULL
);

-- "What did I fire for this ticker recently?" → indexed lookup.
CREATE INDEX IF NOT EXISTS idx_signal_events_lookup
    ON signal_events (ticker_id, computed_at DESC);

-- For pruning / range queries by time.
CREATE INDEX IF NOT EXISTS idx_signal_events_computed
    ON signal_events (computed_at);

-- One row per (ticker, timeframe) — upserted on every compute.
-- Currently audit-only; the app rebuilds engines from raw bars on startup.
CREATE TABLE IF NOT EXISTS ticker_signal_state (
    ticker_id           INTEGER     NOT NULL REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    timeframe           VARCHAR(8)  NOT NULL,
    last_bar_timestamp  TIMESTAMPTZ NOT NULL,
    current_signal      VARCHAR(16) NOT NULL
        CHECK (current_signal IN ('BULLISH', 'BEARISH', 'NEUTRAL')),
    state_json          JSONB       NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker_id, timeframe)
);

INSERT INTO schema_migrations (version) VALUES ('002') ON CONFLICT DO NOTHING;
