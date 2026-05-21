"""instrument_shares time-series table (Phase 2).

Shares-outstanding for every active ticker, sourced primarily from
yfinance. Stored as a TIME SERIES (composite PK `(ticker_id, as_of_date)`)
rather than a single-row-per-ticker shape, so:

  - Corporate actions (splits, bonuses, buybacks, FPOs) that change
    shares-outstanding between refreshes can be retrospectively
    inspected.
  - The XBRL sidecar (deferred — see docs/FUNDAMENTALS_PHASES_1_TO_8.md
    §13) can backfill historical rows alongside yfinance's current
    snapshots without overwriting.
  - Phase 4's "compute historical market cap" can query shares
    as-of the trade date rather than always using today's count.

What this migration adds:
  - instrument_shares — the time-series table itself.

What this migration deliberately does NOT add:
  - A separate `current_shares` view or denormalized table.
    The "latest shares for this ticker" query is
      SELECT shares_outstanding FROM instrument_shares
      WHERE ticker_id = :tid AND as_of_date <= :d
      ORDER BY as_of_date DESC LIMIT 1
    which uses the natural PK index. Materializing this as a separate
    table would be a second write site that can drift; skip.

  - A FK or trigger linking instrument_shares.fetched_at to a
    fundamentals_run row. We rely on the run table being queried
    separately for audit purposes; coupling them at the DB level
    would force every shares-write to also know its run_id, which
    complicates the runner's transaction boundaries.

Grants follow the same pattern as 0006/0007: trip_etl_user writes,
trip_gui_user reads.

Legacy parity: `migrations/legacy/015_instrument_shares.sql`.

Revision ID: 0008_instrument_shares
Revises: 0007_etf_classification
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0008_instrument_shares"
down_revision = "0007_etf_classification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS instrument_shares (
            ticker_id           INTEGER     NOT NULL
                                            REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            as_of_date          DATE        NOT NULL,
            shares_outstanding  BIGINT,
            free_float_factor   REAL,
            shares_source       VARCHAR(24) NOT NULL,
            fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            xbrl_url            TEXT,

            PRIMARY KEY (ticker_id, as_of_date),

            CONSTRAINT instrument_shares_source_chk
                CHECK (shares_source IN
                       ('YFINANCE', 'YFINANCE_IMPLIED',
                        'NSE_XBRL', 'MANUAL_OVERRIDE'))
        );
    """)

    # Index on as_of_date alone for the "all rows from a single
    # quarterly refresh" admin query. Without this, that query
    # full-scans.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_instrument_shares_as_of
            ON instrument_shares (as_of_date DESC);
    """)

    # Partial index: most queries want the LATEST row per ticker.
    # A regular index on (ticker_id, as_of_date DESC) covers this
    # without a partial filter. The PK already covers (ticker_id,
    # as_of_date) ASC; adding a DESC index lets the
    # `ORDER BY as_of_date DESC LIMIT 1` lookup be a single index
    # backward-scan rather than a sort.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_instrument_shares_ticker_desc
            ON instrument_shares (ticker_id, as_of_date DESC);
    """)

    op.execute("""
        COMMENT ON TABLE instrument_shares IS
            'Per-ticker shares-outstanding history. One row per refresh '
            '(as_of_date = the day yfinance was queried, since yfinance '
            'does not expose the filing-date the number is sourced from). '
            'For dates older than the earliest row, query yfinance''s '
            'historical market cap series directly; this table is forward-'
            'looking from when Phase 2 went live.';
    """)
    op.execute("""
        COMMENT ON COLUMN instrument_shares.shares_source IS
            'YFINANCE = direct sharesOutstanding field; YFINANCE_IMPLIED '
            '= back-derived from marketCap/price when direct field was '
            'NULL (lower trust); NSE_XBRL = future XBRL sidecar '
            'cross-validation; MANUAL_OVERRIDE = operator-set.';
    """)
    op.execute("""
        COMMENT ON COLUMN instrument_shares.free_float_factor IS
            'Free-float fraction (0.0-1.0). NULL until a real source '
            'populates it. yfinance does not provide this for Indian '
            'tickers; XBRL sidecar can compute it from promoter holding.';
    """)

    # Grants.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON instrument_shares
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT ON instrument_shares TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS instrument_shares;")
