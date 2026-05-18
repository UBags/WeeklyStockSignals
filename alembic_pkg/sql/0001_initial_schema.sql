-- ============================================================
-- from: migrations/legacy/001_initial_schema.sql
-- ============================================================
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

-- ============================================================
-- from: migrations/legacy/002_signal_state.sql
-- ============================================================
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

-- ============================================================
-- from: migrations/legacy/003_subscribers.sql
-- ============================================================
-- 003_subscribers.sql
-- Notification distribution: who wants what, sent on what cadence, and the
-- audit log of every batch we've delivered.

CREATE TABLE IF NOT EXISTS subscribers (
    subscriber_id           SERIAL       PRIMARY KEY,
    transport               VARCHAR(16)  NOT NULL
        CHECK (transport IN ('email', 'webhook', 'slack', 'sms')),
    address                 TEXT         NOT NULL,
    notify_cadence_minutes  INTEGER      NOT NULL
        CHECK (notify_cadence_minutes BETWEEN 15 AND 1440
               AND notify_cadence_minutes % 15 = 0),
    last_notified_at        TIMESTAMPTZ,
    is_active               BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_subscribers_active_due
    ON subscribers (last_notified_at) WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id   SERIAL       PRIMARY KEY,
    subscriber_id     INTEGER      NOT NULL REFERENCES subscribers(subscriber_id) ON DELETE CASCADE,
    ticker_id         INTEGER      REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    -- NULL ticker_id => "all tickers"
    signal_filter     VARCHAR(64),
    -- NULL signal_filter => "all signals"; otherwise comma-separated list, e.g. 'BUY,SELL'
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Postgres treats NULLs as distinct in unique constraints; use NULLS NOT DISTINCT
    -- (PG 15+) so (sub_id, NULL, NULL) only inserts once.
    CONSTRAINT subscriptions_unique UNIQUE NULLS NOT DISTINCT
        (subscriber_id, ticker_id, signal_filter)
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_by_subscriber
    ON subscriptions (subscriber_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_by_ticker
    ON subscriptions (ticker_id) WHERE ticker_id IS NOT NULL;

-- Notification log: append-only, partitioned by month for cheap pruning later
-- (we keep forever for now per spec, but partitioning gives us the option).
CREATE TABLE IF NOT EXISTS notification_log (
    log_id              BIGSERIAL    NOT NULL,
    subscriber_id       INTEGER      NOT NULL REFERENCES subscribers(subscriber_id) ON DELETE CASCADE,
    event_id            BIGINT       NOT NULL REFERENCES signal_events(event_id) ON DELETE CASCADE,
    notification_count  INTEGER      NOT NULL DEFAULT 1
        CHECK (notification_count >= 1),
    last_sent_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Partition key must be part of PK in a partitioned table.
    PRIMARY KEY (log_id, last_sent_at)
) PARTITION BY RANGE (last_sent_at);

-- Per-subscriber-per-event uniqueness: enforced as a unique index that
-- spans partitions (allowed since PG 11+ when the partition key is included).
CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_log_dedup
    ON notification_log (subscriber_id, event_id, last_sent_at);

-- Lookups when computing "have I notified this subscriber about this event
-- already, and how many times?" during batch composition.
CREATE INDEX IF NOT EXISTS idx_notification_log_subscriber_event
    ON notification_log (subscriber_id, event_id);

INSERT INTO schema_migrations (version) VALUES ('003') ON CONFLICT DO NOTHING;

-- ============================================================
-- from: migrations/legacy/004_partitions_seed.sql
-- ============================================================
-- 004_partitions_seed.sql
-- Bootstrap monthly partitions for notification_log and provide a function
-- to add future partitions. The scheduler can call this function (or an
-- ops cron) to ensure the current and next month always have partitions.

CREATE OR REPLACE FUNCTION ensure_notification_log_partition(p_month DATE)
RETURNS VOID AS $$
DECLARE
    partition_name TEXT;
    start_ts       TIMESTAMPTZ;
    end_ts         TIMESTAMPTZ;
BEGIN
    -- Truncate to month start
    start_ts := DATE_TRUNC('month', p_month);
    end_ts   := start_ts + INTERVAL '1 month';
    partition_name := 'notification_log_' || TO_CHAR(start_ts, 'YYYY_MM');

    EXECUTE FORMAT(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF notification_log
            FOR VALUES FROM (%L) TO (%L)',
        partition_name, start_ts, end_ts
    );
END;
$$ LANGUAGE plpgsql;

-- Seed partitions: previous month, current month, and next two months.
-- Gives a comfortable forward buffer; the app should call
-- ensure_notification_log_partition() periodically to keep extending.
SELECT ensure_notification_log_partition((NOW() - INTERVAL '1 month')::DATE);
SELECT ensure_notification_log_partition(NOW()::DATE);
SELECT ensure_notification_log_partition((NOW() + INTERVAL '1 month')::DATE);
SELECT ensure_notification_log_partition((NOW() + INTERVAL '2 months')::DATE);

INSERT INTO schema_migrations (version) VALUES ('004') ON CONFLICT DO NOTHING;

-- ============================================================
-- from: migrations/legacy/005_drop_ticker_signal_state.sql
-- ============================================================
-- 005_drop_ticker_signal_state.sql
-- Per the Phase 6 decision: ticker_signal_state is dropped.
-- The system rebuilds engine state from raw OHLCV bars on startup (Option A,
-- decided earlier), so this audit table provides no operational value and
-- only generates write amplification on every compute pass.

DROP TABLE IF EXISTS ticker_signal_state;

INSERT INTO schema_migrations (version) VALUES ('005') ON CONFLICT DO NOTHING;

-- ============================================================
-- from: migrations/legacy/006_bars_updated_at.sql
-- ============================================================
-- 006_bars_updated_at.sql
-- Track when each bar was last written/revised. The ComputeScheduler uses
-- this to detect historical revisions — if the ETL rewrites a bar from a
-- prior week (e.g., due to a corporate action or exchange data correction),
-- the engine needs to re-prime from scratch for that ticker rather than
-- silently miss the change.

ALTER TABLE ohlcv_bars
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Trigger to auto-update on UPDATE.
CREATE OR REPLACE FUNCTION _set_ohlcv_bars_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ohlcv_bars_updated_at ON ohlcv_bars;
CREATE TRIGGER trg_ohlcv_bars_updated_at
    BEFORE UPDATE ON ohlcv_bars
    FOR EACH ROW
    EXECUTE FUNCTION _set_ohlcv_bars_updated_at();

-- Index on updated_at lets the scheduler efficiently check
-- "any bars updated since I last looked?"
CREATE INDEX IF NOT EXISTS idx_bars_updated_at
    ON ohlcv_bars (ticker_id, timeframe, updated_at);

INSERT INTO schema_migrations (version) VALUES ('006') ON CONFLICT DO NOTHING;

-- ============================================================
-- from: migrations/legacy/007_ticker_tz_source_timeframe.sql
-- ============================================================
-- 007_ticker_tz_source_timeframe.sql
-- Phase 8 B4: per-ticker timezone and source timeframe.
--
-- `tz` is the exchange's local timezone (IANA name, e.g. 'Asia/Kolkata').
--    Used to compute "week start" for aggregation. Different markets'
--    weeks start at different UTC instants (Monday 00:00 IST is Sunday
--    18:30 UTC); we honor each exchange's local Monday.
--
-- `source_timeframe` is the resolution at which the ETL writes bars
--    for this ticker — typically smaller than the engine's target
--    timeframe so the engine can aggregate intra-week.
--
-- Existing rows default to 'UTC' / '1W' so they continue working as
-- they did before (preserves test fixtures and any seeded data).

ALTER TABLE tickers
    ADD COLUMN IF NOT EXISTS tz TEXT NOT NULL DEFAULT 'UTC';

ALTER TABLE tickers
    ADD COLUMN IF NOT EXISTS source_timeframe TEXT NOT NULL DEFAULT '1W';

-- Validate source_timeframe values are sensible. Constraint is a
-- soft guarantee; the application layer enforces the allowed set
-- via the Timeframe enum.
ALTER TABLE tickers
    ADD CONSTRAINT tickers_source_timeframe_chk
    CHECK (source_timeframe IN ('15m', '1h', '2h', '1D', '1W'));

INSERT INTO schema_migrations (version) VALUES ('007') ON CONFLICT DO NOTHING;

-- ============================================================
-- from: migrations/legacy/008_drop_notification_log_dedup_index.sql
-- ============================================================
-- 008_drop_notification_log_dedup_index.sql
-- Phase 9 C1: drop the misleading UNIQUE index on notification_log.
--
-- The index was created in migration 003 with the intent of enforcing
-- per-(subscriber, event) deduplication at the DB level:
--
--     CREATE UNIQUE INDEX idx_notification_log_dedup
--         ON notification_log (subscriber_id, event_id, last_sent_at);
--
-- But because `last_sent_at` is part of the unique tuple AND defaults to
-- NOW() on insert, every insert gets a microsecond-distinct timestamp,
-- so the constraint never actually triggers. It enforces nothing.
--
-- Real dedup happens at the application layer:
--   1. `find_pending_for_subscriber` (in signals_repo.py) counts existing
--      rows for (subscriber_id, event_id) and caps at 3 per event.
--   2. The Redis queue's Lua-atomic enqueue with the dedup hash prevents
--      double-enqueue of the same (subscriber, event) while one is in
--      flight.
--
-- The misleading index is dropped here. The lookup index
-- `idx_notification_log_subscriber_event` (created in the same migration
-- 003) remains in place — it powers the count query that enforces the
-- 3-send cap.

DROP INDEX IF EXISTS idx_notification_log_dedup;

INSERT INTO schema_migrations (version) VALUES ('008') ON CONFLICT DO NOTHING;

-- ============================================================
-- from: migrations/legacy/010_etl_schema.sql
-- ============================================================
-- Migration 010 — ETL layer schema.
--
-- Adds the columns and tables needed to track NSE listings, run the
-- yfinance-driven ETL, and audit historical bar adjustments.
--
-- Aligns with the existing `tickers` / `ohlcv_bars` schema. The ETL
-- writes to these tables; trip_ema reads from them. The existing
-- ARCHITECTURE.md invariant 3.1 ("ETL is the source of truth for bar
-- availability") makes the new tables' role explicit: the ETL OWNS
-- the data path; the app trusts what's there.

BEGIN;

-- ---------- tickers: enrichment columns ---------- --

ALTER TABLE tickers
    ADD COLUMN IF NOT EXISTS isin             CHAR(12),
    ADD COLUMN IF NOT EXISTS instrument_type  VARCHAR(8) NOT NULL DEFAULT 'EQ',
    ADD COLUMN IF NOT EXISTS lot_size         INTEGER,
    ADD COLUMN IF NOT EXISTS listing_date     DATE,
    ADD COLUMN IF NOT EXISTS face_value       NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS delisted_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS tier             SMALLINT NOT NULL DEFAULT 3;

-- Constraint: instrument_type ∈ {EQ, ETF}. Add as a CHECK rather than
-- an ENUM type so future additions (debt, derivatives) don't require
-- ALTER TYPE.
ALTER TABLE tickers
    DROP CONSTRAINT IF EXISTS tickers_instrument_type_check;
ALTER TABLE tickers
    ADD CONSTRAINT tickers_instrument_type_check
    CHECK (instrument_type IN ('EQ', 'ETF', 'DEBT', 'FUT', 'OPT'));

-- Tier must be 1, 2, or 3.
ALTER TABLE tickers
    DROP CONSTRAINT IF EXISTS tickers_tier_check;
ALTER TABLE tickers
    ADD CONSTRAINT tickers_tier_check
    CHECK (tier BETWEEN 1 AND 3);

-- ISIN is the stable identifier across symbol renames. Partial unique
-- index allows multiple NULL ISINs during backfill while still
-- enforcing uniqueness for tickers that have one.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_tickers_isin
    ON tickers (isin) WHERE isin IS NOT NULL;

-- Hot path: ETL ordering by tier within active tickers.
CREATE INDEX IF NOT EXISTS idx_tickers_tier_active
    ON tickers (tier, is_active) WHERE is_active = TRUE AND delisted_at IS NULL;


-- ---------- ohlcv_bars: adjusted close ---------- --

-- yfinance returns both `Close` (raw) and `Adj Close` (corporate-action-
-- adjusted). The engine sees the RAW close (preserving Pine parity per
-- invariant 3.3); we keep adj_close alongside for backtest accuracy.
-- NULLable because pre-migration rows don't have it; new rows always do.
ALTER TABLE ohlcv_bars
    ADD COLUMN IF NOT EXISTS adj_close DOUBLE PRECISION;


-- ---------- corporate_actions ---------- --
--
-- Splits, bonus issues, dividends, and the "candidate adjustment"
-- pseudo-actions that the reconciler creates when historical bars
-- diverge from current yfinance values. The `status` column drives
-- the approve/reject workflow.
CREATE TABLE IF NOT EXISTS corporate_actions (
    action_id        BIGSERIAL PRIMARY KEY,
    ticker_id        INTEGER       NOT NULL REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    action_type      VARCHAR(32)   NOT NULL,
        -- "split", "bonus", "dividend", "candidate_revision"
    effective_date   DATE          NOT NULL,
        -- For "candidate_revision", this is the bar date that diverged.
    ratio_numerator  NUMERIC(20, 6),
    ratio_denominator NUMERIC(20, 6),
        -- 2:1 split → numerator=2, denominator=1
    amount           NUMERIC(20, 6),
        -- For dividends (per share). NULL for splits/bonus.
    source           VARCHAR(32)   NOT NULL DEFAULT 'yfinance',
    status           VARCHAR(16)   NOT NULL DEFAULT 'recorded',
        -- "recorded" (auto, no action needed), "pending_review", "approved", "rejected"
    raw_payload      JSONB,
    detected_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    reviewed_at      TIMESTAMPTZ,
    reviewed_by      VARCHAR(64),
    notes            TEXT
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_ticker
    ON corporate_actions (ticker_id, effective_date DESC);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_pending
    ON corporate_actions (status, detected_at)
    WHERE status = 'pending_review';


-- ---------- etl_runs ---------- --
--
-- One row per ETL invocation. The CLI writes the starting row, updates
-- it on completion. Powers the "are we behind?" dashboard.
CREATE TABLE IF NOT EXISTS etl_runs (
    run_id              BIGSERIAL PRIMARY KEY,
    kind                VARCHAR(32)  NOT NULL,
        -- "listings", "backfill", "eod", "intraday", "tier_recompute", "unpark"
    started_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    outcome             VARCHAR(16),
        -- NULL while in flight; "success" | "partial" | "failed" on completion.
        -- "partial" = some tickers parked but the run completed.
    tickers_attempted   INTEGER      NOT NULL DEFAULT 0,
    tickers_succeeded   INTEGER      NOT NULL DEFAULT 0,
    tickers_failed      INTEGER      NOT NULL DEFAULT 0,
    bars_written        INTEGER      NOT NULL DEFAULT 0,
    correlation_id      VARCHAR(64),
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_etl_runs_recent
    ON etl_runs (started_at DESC);


-- ---------- etl_ticker_status ---------- --
--
-- Per-ticker bookkeeping. The PySide GUI (Slice 2) reads this for the
-- ticker table view. The `parked` flag drives the unpark workflow.
CREATE TABLE IF NOT EXISTS etl_ticker_status (
    ticker_id                  INTEGER     PRIMARY KEY REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    last_successful_bar_date   DATE,
    last_attempt_at            TIMESTAMPTZ,
    consecutive_failures       INTEGER     NOT NULL DEFAULT 0,
    parked                     BOOLEAN     NOT NULL DEFAULT FALSE,
    parked_at                  TIMESTAMPTZ,
    parked_reason              TEXT,
    last_error                 TEXT,
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_etl_ticker_status_parked
    ON etl_ticker_status (parked, parked_at DESC) WHERE parked = TRUE;

CREATE INDEX IF NOT EXISTS idx_etl_ticker_status_stale
    ON etl_ticker_status (last_successful_bar_date NULLS FIRST);


-- ---------- symbol_history ---------- --
--
-- NSE periodically renames symbols (mergers, rebranding). The ISIN
-- stays stable; we follow it. This table records each rename so
-- historical signals tied to old symbols remain queryable.
CREATE TABLE IF NOT EXISTS symbol_history (
    history_id      BIGSERIAL PRIMARY KEY,
    ticker_id       INTEGER      NOT NULL REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    old_symbol      VARCHAR(32)  NOT NULL,
    new_symbol      VARCHAR(32)  NOT NULL,
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    detected_via    VARCHAR(32)  NOT NULL DEFAULT 'isin_continuity'
);

CREATE INDEX IF NOT EXISTS idx_symbol_history_old
    ON symbol_history (old_symbol);

CREATE INDEX IF NOT EXISTS idx_symbol_history_ticker
    ON symbol_history (ticker_id, changed_at DESC);


INSERT INTO schema_migrations (version) VALUES ('010') ON CONFLICT DO NOTHING;

COMMIT;
