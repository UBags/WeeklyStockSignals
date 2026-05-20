-- Migration 012 — signal_backtest_runs table + etl_runs.exchange column.
--
-- Phase 15a: introduces the backtest signal-generation pipeline.
--
-- IMPORTANT — Phase 12 transition note:
--
-- This file is the legacy-migrator parallel of Alembic revision
-- 0005_signal_backtest_runs. The legacy migrator is NOT run against
-- production databases anymore (Phase 12 moved fresh-DB setup to
-- Alembic and stamps pre-existing prod DBs at the 0001 squash
-- point). This file exists for ONE reason: the parity test in
-- tests/persistence/test_legacy_migrator.py runs both migration
-- paths against fresh databases and asserts the resulting table
-- sets are identical. Without this file, the legacy fresh-DB path
-- would not have signal_backtest_runs and the parity test would
-- fail.
--
-- DO NOT remove this file under the assumption that "legacy is
-- dead." It's load-bearing for the parity test, which is the
-- guard that catches schema drift between the two paths.
--
-- The schema, constraints, grants, and the etl_runs ALTER below
-- MUST stay bit-equivalent to alembic_pkg/versions/0005_signal_backtest_runs.py.
-- If you change one, change the other in the same commit.

BEGIN;

-- ---------- 1. etl_runs.exchange column ---------- --
--
-- NULL-able: pre-Phase-15 rows don't know which exchange they were
-- for. New rows from updated call sites populate it via the new
-- optional `exchange=` parameter on EtlStatusRepository.open_run.

ALTER TABLE etl_runs
    ADD COLUMN IF NOT EXISTS exchange TEXT;

COMMENT ON COLUMN etl_runs.exchange IS
    'Exchange the ETL operation targeted. NULL for pre-Phase-15 rows. '
    'New code populates it via EtlStatusRepository.open_run(exchange=...). '
    'Used by the signal_backtest concurrent-run guard to scope by exchange.';


-- ---------- 2. signal_backtest_runs table ---------- --

CREATE TABLE IF NOT EXISTS signal_backtest_runs (
    exchange                TEXT         NOT NULL,
    ticker_id               INTEGER      NOT NULL
        REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    timeframe               TEXT         NOT NULL,
    computed_from_date      DATE         NOT NULL,
    computed_through_date   DATE         NOT NULL,
    computed_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_run_id             BIGINT,

    PRIMARY KEY (exchange, ticker_id, timeframe),

    CONSTRAINT signal_backtest_runs_date_order_chk
        CHECK (computed_through_date >= computed_from_date),

    CONSTRAINT signal_backtest_runs_timeframe_chk
        CHECK (timeframe IN ('1D', '1W'))
);

CREATE INDEX IF NOT EXISTS idx_signal_backtest_runs_exchange
    ON signal_backtest_runs (exchange);

COMMENT ON TABLE signal_backtest_runs IS
    'Per-(exchange, ticker_id, timeframe) resume tracking for backtest '
    'signal generation. Allows incremental runs to skip dates already '
    'computed. force_full=true wipes rows for the affected exchange '
    'in a small transaction at run start.';


-- ---------- Grants ---------- --
--
-- Mirrors the per-role pattern from 0002_db_roles. ETL writes (the
-- signal generator inserts rows and force_full deletes them);
-- engine and GUI get read-only.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLE signal_backtest_runs TO trip_etl_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_engine_user') THEN
        GRANT SELECT ON TABLE signal_backtest_runs TO trip_engine_user;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
        GRANT SELECT ON TABLE signal_backtest_runs TO trip_gui_user;
    END IF;
END
$$;


-- Record this migration as applied (legacy migrator convention).
INSERT INTO schema_migrations (version) VALUES ('012') ON CONFLICT DO NOTHING;

COMMIT;
