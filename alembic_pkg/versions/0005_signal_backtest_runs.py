"""signal_backtest_runs table + etl_runs.exchange column

Phase 15a: introduces the backtest signal-generation pipeline.
Two schema changes in one migration:

1. **`signal_backtest_runs`** (NEW table). Per-(exchange, ticker_id,
   timeframe) resume tracking for backtest signal generation. One row
   per (exchange, ticker, timeframe) ever computed; the resume runner
   reads `computed_through_date` to decide where to pick up. The
   `force_full=true` operator workflow wipes rows for the affected
   exchange in a small dedicated transaction at run start.

2. **`etl_runs.exchange`** (NEW column). Pre-Phase-15, etl_runs had no
   exchange column — the kind+correlation_id was enough to interpret
   any given row. Phase 15 introduces an async ETL operation
   (kind='signal_backtest') whose concurrent-run guard needs to ask
   "is a signal_backtest already in flight FOR THIS EXCHANGE?", and a
   guard that ignores exchange would serialize NSE and BSE backtests
   for no good reason. The column is NULL-able so pre-Phase-15 rows
   remain valid; new callers populate it via the new optional
   `exchange=` parameter on `EtlStatusRepository.open_run`. As of
   Phase 15a, all existing call sites (refresh_listings,
   recompute_tiers, run_eod, run_intraday, run_backfill) are updated
   to pass exchange through, so going forward every row has an
   exchange except the pre-existing ones.

Why one migration, not two:
  - Conceptually paired: the new table tracks per-exchange resume
    state, and the column makes per-exchange in-flight detection
    possible. They land together as the schema foundation for
    Phase 15.
  - One legacy parity file (012_signal_backtest_runs.sql) covers
    both, keeping the parity test simple.

Grants (signal_backtest_runs):
  - trip_etl_user: SELECT, INSERT, UPDATE, DELETE (the signal
    generator writes; force_full wipes rows for an exchange)
  - trip_engine_user: SELECT
  - trip_gui_user: SELECT

Grants (etl_runs.exchange): no per-column grants. Existing
table-level grants on etl_runs already cover the new column.

Legacy parity:
  `migrations/legacy/012_signal_backtest_runs.sql` is the
  bit-equivalent parallel. The parity test compares table sets,
  but per the README the SQL must stay column-equivalent too —
  hence the parallel ALTER on etl_runs in the legacy file.

Revision ID: 0005_signal_backtest_runs
Revises: 0004_exchange_state
Create Date: 2026-05-20
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0005_signal_backtest_runs"
down_revision = "0004_exchange_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. etl_runs.exchange column ---- #
    # NULL-able: pre-Phase-15 rows don't know which exchange they
    # were for. New rows from updated call sites will populate it.
    op.execute("""
        ALTER TABLE etl_runs
            ADD COLUMN IF NOT EXISTS exchange TEXT;
    """)

    op.execute("""
        COMMENT ON COLUMN etl_runs.exchange IS
            'Exchange the ETL operation targeted. NULL for pre-Phase-15 rows. '
            'New code populates it via EtlStatusRepository.open_run(exchange=...). '
            'Used by the signal_backtest concurrent-run guard to scope by exchange.';
    """)

    # ---- 2. signal_backtest_runs table ---- #
    op.execute("""
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
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_backtest_runs_exchange
            ON signal_backtest_runs (exchange);
    """)

    op.execute("""
        COMMENT ON TABLE signal_backtest_runs IS
            'Per-(exchange, ticker_id, timeframe) resume tracking for backtest '
            'signal generation. Allows incremental runs to skip dates already '
            'computed. force_full=true wipes rows for the affected exchange '
            'in a small transaction at run start.';
    """)

    # ---- Grants (table-level) ---- #
    op.execute("""
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
    """)


def downgrade() -> None:
    """Drop the table and the column.

    Loses all resume state — operators must use force_full on the
    next run to recompute. Acceptable because resume state is a
    pure optimization; correctness doesn't depend on it.
    """
    op.execute("DROP TABLE IF EXISTS signal_backtest_runs;")
    op.execute("ALTER TABLE etl_runs DROP COLUMN IF EXISTS exchange;")
