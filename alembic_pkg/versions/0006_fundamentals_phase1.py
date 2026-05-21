"""instrument_industry + industry_override + fundamentals_run

Phase 1 of the fundamentals/indices vertical slice
(see docs/FUNDAMENTALS_PHASES_1_TO_8.md).

Three new tables, no changes to existing tables:

1. **instrument_industry** — one row per ticker_id. Stores yfinance's
   sector + industry classification (or whatever source replaces
   yfinance later). The `classification_source` column records
   provenance so analytics can filter by trust level.

2. **industry_override** — operator-supplied gap-fillers. When an
   override row exists, the runner never overwrites it. Used for
   tickers yfinance gets wrong or doesn't classify.

3. **fundamentals_run** — discriminated by `phase` ('industry' for
   Phase 1; 'shares', 'financials', 'market_cap', 'index' for later
   phases). Tracks one row per refresh invocation: when it started,
   how it ended, how many tickers succeeded/failed/skipped, and an
   opaque `progress_json` blob the runner uses to resume from a
   crash or rate-limit pause.

Why one fundamentals_run table for all phases instead of one per
phase: each phase is the same shape — a long-running fetch loop
scoped to an exchange. Sharing the table keeps the operations
dashboard simple. Discriminator is `phase`.

Why no `last_industry_refresh_per_exchange` column on tickers or
elsewhere: it's derivable from
  `MAX(completed_at) FROM fundamentals_run
   WHERE phase='industry' AND exchange=X AND status='SUCCESS'`
Denormalizing it would create a second write site that can drift.

Grants: trip_etl_user gets INSERT/UPDATE/DELETE/SELECT on all three
(it does the refresh writes); trip_gui_user gets SELECT (it serves
the read endpoints and the GUI display).

Legacy parity: `migrations/legacy/013_fundamentals_phase1.sql` is the
bit-equivalent parallel.

Revision ID: 0006_fundamentals_phase1
Revises: 0005_signal_backtest_runs
Create Date: 2026-05-21
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0006_fundamentals_phase1"
down_revision = "0005_signal_backtest_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. instrument_industry ---- #
    op.execute("""
        CREATE TABLE IF NOT EXISTS instrument_industry (
            ticker_id              INTEGER     PRIMARY KEY
                                               REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            sector                 VARCHAR(64),
            industry               VARCHAR(128),
            classification_source  VARCHAR(32) NOT NULL DEFAULT 'YFINANCE',
            fetched_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT instrument_industry_source_chk
                CHECK (classification_source IN
                       ('YFINANCE', 'GROWW', 'MANUAL_OVERRIDE', 'NSE_SECTORAL'))
        );
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_instrument_industry_industry
            ON instrument_industry (industry);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_instrument_industry_sector
            ON instrument_industry (sector);
    """)

    op.execute("""
        COMMENT ON TABLE instrument_industry IS
            'Per-ticker sector/industry classification. Source recorded '
            'in classification_source. The runner never overwrites a row '
            'that has an industry_override entry for the same ticker_id.';
    """)

    # ---- 2. industry_override ---- #
    op.execute("""
        CREATE TABLE IF NOT EXISTS industry_override (
            ticker_id    INTEGER     PRIMARY KEY
                                     REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            sector       VARCHAR(64)  NOT NULL,
            industry     VARCHAR(128) NOT NULL,
            note         TEXT,
            set_by       VARCHAR(64),
            set_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

    op.execute("""
        COMMENT ON TABLE industry_override IS
            'Operator-supplied industry/sector for tickers the auto-'
            'classifier gets wrong or misses. Presence of a row here '
            'means the runner will not touch that ticker_id''s '
            'instrument_industry entry on subsequent refreshes.';
    """)

    # ---- 3. fundamentals_run ---- #
    op.execute("""
        CREATE TABLE IF NOT EXISTS fundamentals_run (
            run_id        BIGSERIAL    PRIMARY KEY,
            phase         VARCHAR(32)  NOT NULL,
            exchange      VARCHAR(16),
            started_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            completed_at  TIMESTAMPTZ,
            status        VARCHAR(16)  NOT NULL DEFAULT 'RUNNING',
            succeeded     INTEGER      NOT NULL DEFAULT 0,
            failed        INTEGER      NOT NULL DEFAULT 0,
            skipped       INTEGER      NOT NULL DEFAULT 0,
            progress_json JSONB,
            notes         TEXT,

            CONSTRAINT fundamentals_run_phase_chk
                CHECK (phase IN ('industry', 'shares', 'financials',
                                 'market_cap', 'index_build', 'index_value',
                                 'index_indicators')),
            CONSTRAINT fundamentals_run_status_chk
                CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED'))
        );
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_fundamentals_run_phase_ex
            ON fundamentals_run (phase, exchange, started_at DESC);
    """)
    # For the common query "latest completed run for this phase+exchange",
    # which is how the quarterly scheduler decides whether to skip.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_fundamentals_run_completed
            ON fundamentals_run (phase, exchange, completed_at DESC)
            WHERE status = 'SUCCESS';
    """)

    op.execute("""
        COMMENT ON TABLE fundamentals_run IS
            'One row per fundamentals refresh invocation. phase '
            'discriminates which fundamentals data was being refreshed; '
            'progress_json is an opaque JSONB blob the runner uses to '
            'resume from a crash or rate-limit pause.';
    """)

    # ---- Grants ---- #
    # trip_etl_user does the writes (running the refreshes); trip_gui_user
    # serves the read endpoints. Match the role grants in 0002_db_roles.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON instrument_industry, industry_override, fundamentals_run
                    TO trip_etl_user;
                GRANT USAGE, SELECT ON SEQUENCE fundamentals_run_run_id_seq
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT
                    ON instrument_industry, industry_override, fundamentals_run
                    TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    # No FK ordering tricks needed; tables are independent of each other.
    # Drop in reverse-creation order to be conventional.
    op.execute("DROP TABLE IF EXISTS fundamentals_run;")
    op.execute("DROP TABLE IF EXISTS industry_override;")
    op.execute("DROP TABLE IF EXISTS instrument_industry;")
