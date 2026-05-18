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
