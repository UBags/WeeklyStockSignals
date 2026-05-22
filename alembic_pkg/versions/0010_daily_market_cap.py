"""daily_market_cap time-series table (Phase 4A).

One row per (ticker, trade_date) recording the market capitalization
for that ticker on that day. This is the foundation for Phase 4B's
industry index construction.

Source policy (encoded in the `source` column):

  COMPUTED              — close × shares_outstanding, where shares
                          came from instrument_shares.get_as_of(date).
                          The trusted path.

  YFINANCE_HIST_MCAP    — pulled from yfinance's historical market
                          cap series. Used for trade_dates before
                          our instrument_shares data starts (we
                          have shares only from late May 2026
                          onward; pre-Nov 2025 trade dates need
                          the historical series). Less precise than
                          COMPUTED but available for long backfills.

  APPROX_TODAYS_SHARES  — close × current shares-outstanding, used
                          ONLY for dates where shares-outstanding
                          is unknown for the date but known today.
                          This is wrong for any company that has
                          done a split / bonus / buyback in the
                          gap; analytics should treat these rows
                          as low-trust.

The `shares_used` column records the actual shares count multiplied
in, so analytics can spot-check the math without re-deriving from
instrument_shares. NULL when source = YFINANCE_HIST_MCAP (yfinance
gives us the mcap directly, not the shares used to derive it).

What this migration adds:
  - daily_market_cap table.

What this migration deliberately does NOT add:
  - A separate "current_market_cap" view. Phase 4B's index
    construction reads the latest trade_date per ticker, which is
    the natural use of the (ticker_id, trade_date DESC) part of
    the PK ordering.
  - free_float_adjusted_market_cap. We use full market cap; this
    is consistent with the §2 design decision. If/when free-float
    becomes available (via the deferred XBRL sidecar), it would be
    a separate column or table.

Storage scale: ~2350 EQ tickers × ~250 trading days/year ≈ 590k
rows per year of backfill. 1-year backfill (the operator's choice
for Phase 4A) → 590k rows. Postgres handles this trivially with
the indexes below.

Legacy parity: `migrations/legacy/017_daily_market_cap.sql`.

Revision ID: 0010_daily_market_cap
Revises: 0009_quarterly_financials
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0010_daily_market_cap"
down_revision = "0009_quarterly_financials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS daily_market_cap (
            ticker_id    INTEGER          NOT NULL
                                          REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            trade_date   DATE             NOT NULL,
            close_price  DOUBLE PRECISION NOT NULL,
            shares_used  BIGINT,
            market_cap   NUMERIC(22, 2)   NOT NULL,
            source       VARCHAR(24)      NOT NULL,
            computed_at  TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

            PRIMARY KEY (ticker_id, trade_date),

            CONSTRAINT daily_market_cap_source_chk
                CHECK (source IN
                       ('COMPUTED', 'YFINANCE_HIST_MCAP',
                        'APPROX_TODAYS_SHARES', 'MANUAL_OVERRIDE')),
            CONSTRAINT daily_market_cap_positive
                CHECK (market_cap >= 0)
        );
    """)

    # Date-axis index: Phase 4B's "top-N tickers by mcap on date X"
    # needs to scan many tickers for one date. Without this, that
    # query would do per-ticker lookups via the PK.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_daily_market_cap_date_desc
            ON daily_market_cap (trade_date, market_cap DESC);
    """)

    op.execute("""
        COMMENT ON TABLE daily_market_cap IS
            'Per-ticker daily market cap, derived from close × shares '
            'or pulled from yfinance historical mcap. The source column '
            'records the derivation; analytics can filter on trust level.';
    """)
    op.execute("""
        COMMENT ON COLUMN daily_market_cap.source IS
            'COMPUTED = close × shares_outstanding (trusted); '
            'YFINANCE_HIST_MCAP = yfinance''s pre-baked historical mcap '
            '(used for dates before our instrument_shares starts); '
            'APPROX_TODAYS_SHARES = close × today''s shares (wrong if '
            'corporate actions intervened — low trust, marked for filter); '
            'MANUAL_OVERRIDE = operator-set.';
    """)
    op.execute("""
        COMMENT ON COLUMN daily_market_cap.shares_used IS
            'Actual shares count multiplied to derive market_cap. NULL '
            'when source = YFINANCE_HIST_MCAP (yfinance gives us the '
            'mcap directly, not the shares used to derive it).';
    """)

    # Grants — same pattern as 0006-0009.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON daily_market_cap
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT ON daily_market_cap TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS daily_market_cap;")
