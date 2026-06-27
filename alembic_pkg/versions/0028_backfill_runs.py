"""Migration 0028 — backfill_runs marker table.

V1 cutover Step 11a.

Adds `backfill_runs`, a per-run audit log AND active-marker table:
  - One row per backfill invocation
  - finished_at IS NULL → the backfill is currently running
  - Unique partial index ensures at most one active row at a time
    (a second concurrent backfill INSERT fails with unique violation;
    operator must `backfill --release-lock` the prior orphan first
    if the previous run crashed without setting finished_at)

The live cycle (Step 10's `_make_v1_compute_run_once` /
`_make_v1_distribute_run_once`) gains a marker check on every tick.
When `is_running()` returns True the tick no-ops and spawns a
polling thread (15-min cadence) that runs one catch-up tick after
the backfill completes.

Step 11b adds the actual backfill pipeline + CLI subcommand that
INSERTs into this table.

Legacy parity: migrations/legacy/032_backfill_runs.sql.

Revision ID: 0028_backfill_runs
Revises:     0027_notif_log_v1_cols
Create Date: 2026-06-27
"""
from __future__ import annotations

from alembic import op


revision = "0028_backfill_runs"
down_revision = "0027_notif_log_v1_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
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
        )
    """)

    # Fast `is_running()` lookup and active-runs enumeration.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_backfill_runs_active
            ON backfill_runs (started_at DESC)
            WHERE finished_at IS NULL
    """)

    # At most ONE active row at a time. A second concurrent INSERT
    # with finished_at IS NULL fails — fail-closed semantics for
    # accidental double-start. Operator clears stale markers via
    # `backfill --release-lock` (Step 11b).
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_backfill_runs_one_active
            ON backfill_runs ((TRUE))
            WHERE finished_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_backfill_runs_one_active")
    op.execute("DROP INDEX IF EXISTS idx_backfill_runs_active")
    op.execute("DROP TABLE IF EXISTS backfill_runs")
