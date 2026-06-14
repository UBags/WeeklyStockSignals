"""quarterly_financials time-series table (Phase 3).

Per-ticker quarterly revenue + EBITDA, sourced from yfinance via
`Ticker.quarterly_income_stmt` / `Ticker.quarterly_financials`.

Composite PK is **(ticker_id, fiscal_quarter, fetched_at)** — every
refresh inserts a new row, so the same (ticker, quarter) pair
accumulates a history of how yfinance has reported that quarter
over time. The motivation:

  - Companies restate quarterly results months after the original
    filing. With a non-time-series PK we'd silently overwrite the
    original number with the restated one and lose evidence that
    the restatement happened.

  - yfinance occasionally corrects its own numbers (data-vendor
    revisions). The time-series shape preserves those revisions.

The vast majority of rows will be duplicates of the same number for
the same (ticker, quarter) pair across multiple refreshes. Storage
cost is real but acceptable: ~2,300 NSE tickers × 4 quarters per
refresh × 4 refreshes per year ≈ 37,000 rows per year. Postgres
laughs.

Reads default to "latest fetched_at per (ticker, quarter)" via
`DISTINCT ON` in the repo. The history lookup uses the full PK
range to expose all snapshots for a given quarter.

What this migration adds:
  - quarterly_financials — the time-series table itself.

What this migration deliberately does NOT add:
  - A separate `current_financials` view. The DISTINCT ON query is
    fast enough at the storage volumes we're discussing.
  - Reported-as-of date columns beyond fetched_at. yfinance doesn't
    expose when the report was originally filed vs restated; we
    only know when WE fetched it. Adding a fictional "filing date"
    column would be misleading.
  - EBITDA computation logic. Phase 3.A stores yfinance's reported
    EBITDA when it exists, NULL otherwise. Phase 3.B (next session)
    will add computation after probing yfinance's actual data shape.

Legacy parity: `migrations/legacy/016_quarterly_financials.sql`.

Revision ID: 0009_quarterly_financials
Revises: 0008_instrument_shares
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0009_quarterly_financials"
down_revision = "0008_instrument_shares"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quarterly_financials (
            ticker_id       INTEGER     NOT NULL
                                        REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            fiscal_quarter  DATE        NOT NULL,
            fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            revenue         NUMERIC(20, 2),
            ebitda          NUMERIC(20, 2),
            currency        VARCHAR(8)  NOT NULL DEFAULT 'INR',
            source          VARCHAR(24) NOT NULL DEFAULT 'YFINANCE',

            PRIMARY KEY (ticker_id, fiscal_quarter, fetched_at),

            CONSTRAINT quarterly_financials_source_chk
                CHECK (source IN ('YFINANCE', 'YFINANCE_COMPUTED',
                                  'NSE_XBRL', 'MANUAL_OVERRIDE'))
        );
    """)

    # Latest snapshot per (ticker, quarter): DISTINCT ON traverses
    # this index backward to find the newest fetched_at per group
    # without a sort.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_quarterly_financials_latest
            ON quarterly_financials (ticker_id, fiscal_quarter, fetched_at DESC);
    """)

    # Time-axis scan: "all rows for fiscal Q3 2025 across the universe"
    # is a Phase 4 analytics query. Without this index, full table
    # scan.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_quarterly_financials_quarter
            ON quarterly_financials (fiscal_quarter DESC);
    """)

    op.execute("""
        COMMENT ON TABLE quarterly_financials IS
            'Per-ticker quarterly revenue + EBITDA. Time-series PK '
            '(ticker_id, fiscal_quarter, fetched_at) — every refresh '
            'inserts a new row, so restatements and yfinance data '
            'revisions are detectable. Reads default to latest '
            'fetched_at per (ticker, quarter) via DISTINCT ON in '
            'the repo.';
    """)
    op.execute("""
        COMMENT ON COLUMN quarterly_financials.fiscal_quarter IS
            'Quarter-end date as reported by yfinance. We do NOT '
            'align to a global calendar — Indian companies file '
            'Mar/Jun/Sep/Dec quarter-ends; some yfinance entries '
            'use calendar Q-ends, some use fiscal. Trust what '
            'yfinance returns.';
    """)
    op.execute("""
        COMMENT ON COLUMN quarterly_financials.source IS
            'YFINANCE = direct from quarterly_income_stmt; '
            'YFINANCE_COMPUTED = derived (e.g. EBITDA from operating '
            'income + D&A — reserved for Phase 3.B); '
            'NSE_XBRL = future XBRL sidecar; '
            'MANUAL_OVERRIDE = operator-set.';
    """)

    # Grants follow the same pattern as 0006-0008.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON quarterly_financials
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT ON quarterly_financials TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quarterly_financials;")
