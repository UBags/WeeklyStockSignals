-- Legacy SQL parity for alembic migration 0028.
-- V1 cutover Step 11a: backfill_runs marker table.
--
-- Mirrors alembic_pkg/versions/0028_backfill_runs.py exactly.

BEGIN;

CREATE TABLE IF NOT EXISTS backfill_runs (
    run_id        BIGSERIAL    PRIMARY KEY,
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    pid           INTEGER      NOT NULL,
    exchange      VARCHAR(8)   NOT NULL,
    date_from     DATE         NOT NULL,
    date_to       DATE         NOT NULL,
    refetch_bars  BOOLEAN      NOT NULL,
    ticker_scope  TEXT         NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backfill_runs_active
    ON backfill_runs (started_at DESC)
    WHERE finished_at IS NULL;

-- At most ONE active row at a time. Operator clears stale markers
-- via `backfill --release-lock` (Step 11b) if the prior run crashed
-- without UPDATE'ing finished_at.
CREATE UNIQUE INDEX IF NOT EXISTS uq_backfill_runs_one_active
    ON backfill_runs ((TRUE))
    WHERE finished_at IS NULL;

COMMIT;
