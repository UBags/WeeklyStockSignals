"""create exchange_state table — per-exchange ETL timestamps

Phase 14.6b: a single small table that records the latest ETL
state for each exchange. One row per exchange. The runner writes
to this table in the same transaction that closes the corresponding
etl_runs row (Option A timing) so a successful row in etl_runs
implies a synchronized exchange_state.

Why a separate table rather than computing it from etl_runs:
  - etl_runs is unbounded and grows. Scanning it for "the latest
    successful EoD for NSE" gets slower over time.
  - etl_runs has no retention guarantee; truncating it must not
    lose operator-visible state.
  - The GUI's pulse view wants O(1) read per exchange. A primary-
    key lookup on exchange_state is exactly that.
  - "Initial load completed at" has no natural representation in
    etl_runs (it's a one-shot lifecycle event, not a recurring run).

Schema:
  exchange                  TEXT PRIMARY KEY
  initial_load_started_at   TIMESTAMPTZ NULL  -- set on first backfill, never overwritten
  initial_load_completed_at TIMESTAMPTZ NULL  -- set on first SUCCESS-outcome backfill, never overwritten
  last_eod_run_at           TIMESTAMPTZ NULL
  last_eod_outcome          TEXT NULL         -- 'success'/'partial'/'failed'
  last_eod_data_date        DATE NULL         -- latest bar date covered
  last_eod_run_id           BIGINT NULL       -- pointer into etl_runs (no FK on purpose)
  last_intraday_run_at      TIMESTAMPTZ NULL
  last_listings_sync_at     TIMESTAMPTZ NULL
  last_listings_run_id      BIGINT NULL
  last_tier_recompute_at    TIMESTAMPTZ NULL
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()

No foreign key to etl_runs.run_id by design: etl_runs has no
retention policy and may be truncated for operational reasons; a
dangling pointer here is acceptable, an FK constraint that blocks
truncation is not.

Seeding:
  Inserts a row for NSE with the values captured at Phase 14.6b
  design time (from the live production DB). The INSERT uses
  ON CONFLICT DO NOTHING so a rerun of this migration against a
  DB where someone manually pre-populated the row is a no-op.

Grants:
  trip_etl_user gets full DML on exchange_state (the runner writes
  here). trip_engine_user and trip_gui_user get SELECT only — the
  pulse endpoint reads it. Mirrors the per-role pattern set in
  0002_db_roles.

Revision ID: 0004_exchange_state
Revises: 0003_drop_source_timeframe
Create Date: 2026-05-19
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0004_exchange_state"
down_revision = "0003_drop_source_timeframe"
branch_labels = None
depends_on = None


# Captured at Phase 14.6b design time from the live NSE production
# database. Stored as ISO-8601 strings; Postgres parses them on
# INSERT. IST = UTC+5:30 is preserved by the TIMESTAMPTZ column.
_NSE_SEED = {
    "exchange": "NSE",
    "initial_load_started_at":   "2026-05-18 22:52:37+05:30",
    "initial_load_completed_at": "2026-05-19 02:09:23+05:30",
    "last_eod_run_at":           "2026-05-20 01:01:21+05:30",
    "last_eod_outcome":          "success",
    "last_eod_data_date":        "2026-05-19",
    "last_eod_run_id":           None,  # not retroactively known
    "last_listings_sync_at":     "2026-05-18 18:44:25+05:30",
    "last_listings_run_id":      None,
}


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS exchange_state (
            exchange                  TEXT        PRIMARY KEY,
            initial_load_started_at   TIMESTAMPTZ,
            initial_load_completed_at TIMESTAMPTZ,
            last_eod_run_at           TIMESTAMPTZ,
            last_eod_outcome          TEXT,
            last_eod_data_date        DATE,
            last_eod_run_id           BIGINT,
            last_intraday_run_at      TIMESTAMPTZ,
            last_listings_sync_at     TIMESTAMPTZ,
            last_listings_run_id      BIGINT,
            last_tier_recompute_at    TIMESTAMPTZ,
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT exchange_state_outcome_chk
                CHECK (
                    last_eod_outcome IS NULL
                    OR last_eod_outcome IN ('success', 'partial', 'failed')
                ),
            CONSTRAINT exchange_state_initial_load_order_chk
                CHECK (
                    initial_load_completed_at IS NULL
                    OR initial_load_started_at IS NULL
                    OR initial_load_completed_at >= initial_load_started_at
                )
        );
    """)

    op.execute("""
        COMMENT ON TABLE exchange_state IS
            'Per-exchange ETL state snapshot. One row per configured exchange. '
            'Written by the runner inside the same transaction that closes etl_runs. '
            'Read by the GUI pulse view and the /v1/admin/exchange-state endpoints.';
    """)

    # Grants — mirror the per-role pattern from 0002_db_roles.
    # ETL writes; engine and GUI read.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON TABLE exchange_state TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_engine_user') THEN
                GRANT SELECT ON TABLE exchange_state TO trip_engine_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT ON TABLE exchange_state TO trip_gui_user;
            END IF;
        END
        $$;
    """)

    # ---- Seed NSE row ---- #
    # ON CONFLICT DO NOTHING so re-running the migration (e.g. via
    # Alembic stamp shenanigans) doesn't fail if the row already
    # exists. The seed values were captured from the live DB at
    # Phase 14.6b design time.
    op.execute(f"""
        INSERT INTO exchange_state (
            exchange,
            initial_load_started_at,
            initial_load_completed_at,
            last_eod_run_at,
            last_eod_outcome,
            last_eod_data_date,
            last_eod_run_id,
            last_listings_sync_at,
            last_listings_run_id,
            updated_at
        ) VALUES (
            '{_NSE_SEED["exchange"]}',
            '{_NSE_SEED["initial_load_started_at"]}',
            '{_NSE_SEED["initial_load_completed_at"]}',
            '{_NSE_SEED["last_eod_run_at"]}',
            '{_NSE_SEED["last_eod_outcome"]}',
            '{_NSE_SEED["last_eod_data_date"]}',
            NULL,
            '{_NSE_SEED["last_listings_sync_at"]}',
            NULL,
            NOW()
        )
        ON CONFLICT (exchange) DO NOTHING;
    """)


def downgrade() -> None:
    """Drop the table. Loses all per-exchange state — operator must
    rebuild via running the next EoD pass for each exchange.

    Acceptable because the table is a denormalized view: the
    underlying truth lives in etl_runs (for run timestamps) and in
    ohlcv_bars (for last_eod_data_date). It just won't have the
    initial_load_* fields, which are intrinsic to exchange_state.
    """
    op.execute("DROP TABLE IF EXISTS exchange_state;")
