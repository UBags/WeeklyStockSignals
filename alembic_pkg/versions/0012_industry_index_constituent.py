"""industry_index_constituent table (Phase 4B-1).

Membership of an industry index over time. One row per
(index, ticker, effective_period). When the rebalance picks a
new constituent, a new row is opened with effective_to=NULL.
When a constituent is dropped at next rebalance, effective_to
gets set to the rebalance date minus one day.

What this migration adds:
  - industry_index_constituent — membership-with-history.

Columns:
  - industry_name: FK to industry_index.
  - ticker_id: FK to tickers.
  - rebalance_date: when this constituent was picked. Same value
    appears across all constituents picked at the same rebalance.
  - effective_from / effective_to: when this constituent is "in"
    the index. effective_from = rebalance_date; effective_to is
    NULL while active, set to (next_rebalance - 1 day) when
    superseded, or to the delisted_at date for mid-cycle delisting.
  - weight_at_rebalance: the fixed weight assigned at the
    rebalance. Sum of weights across all constituents picked at
    one rebalance is 1.0 (modulo float rounding). Mid-cycle
    delistings DO NOT mutate this — they are handled at
    value-computation time via drop-and-renormalize.
  - market_cap_at_rebalance: the constituent's mcap on the
    rebalance date. Stored for audit and for "what was its weight
    when it was picked".
  - close_at_rebalance: the constituent's close price on the
    rebalance date. Stored because the modified Laspeyres formula
    references close_i(t_rebalance) for every value computation;
    looking it up via daily_market_cap on every compute would be
    wasteful.

Time-axis semantics:
  - A ticker can have MULTIPLE rows for the same industry_name,
    one per period it was a constituent. Read queries default to
    "where now BETWEEN effective_from AND COALESCE(effective_to,
    today)" semantics.
  - effective_from / effective_to ranges within one industry should
    never overlap for the same ticker; the runner enforces this
    invariant at write time. We don't enforce it via DB CHECK
    because Postgres exclusion constraints get awkward.

Composite PK is (industry_name, ticker_id, effective_from). The
effective_from inclusion is what lets us have history.

Indexes:
  - idx_industry_index_constituent_active for the hot query
    "who's in index X right now" via WHERE effective_to IS NULL.
  - idx_industry_index_constituent_asof for "who was in index X
    on date D" via (industry_name, effective_from) walks.

Legacy parity: migrations/legacy/019_industry_index_constituent.sql

Revision ID: 0012_industry_index_constituent
Revises: 0011_industry_index
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


revision = "0012_industry_index_constituent"
down_revision = "0011_industry_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS industry_index_constituent (
            industry_name           VARCHAR(128)     NOT NULL
                                                    REFERENCES industry_index(industry_name) ON DELETE CASCADE,
            ticker_id               INTEGER          NOT NULL
                                                    REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            rebalance_date          DATE             NOT NULL,
            effective_from          DATE             NOT NULL,
            effective_to            DATE,
            weight_at_rebalance     DOUBLE PRECISION NOT NULL,
            market_cap_at_rebalance NUMERIC(22, 2)   NOT NULL,
            close_at_rebalance      DOUBLE PRECISION NOT NULL,
            removed_reason          VARCHAR(32),

            PRIMARY KEY (industry_name, ticker_id, effective_from),

            CONSTRAINT iic_weight_chk
                CHECK (weight_at_rebalance >= 0 AND weight_at_rebalance <= 1),
            CONSTRAINT iic_mcap_chk
                CHECK (market_cap_at_rebalance > 0),
            CONSTRAINT iic_close_chk
                CHECK (close_at_rebalance > 0),
            CONSTRAINT iic_range_chk
                CHECK (effective_to IS NULL OR effective_to >= effective_from),
            CONSTRAINT iic_removed_reason_chk
                CHECK (removed_reason IS NULL OR removed_reason IN
                       ('REBALANCE_EXIT', 'DELISTED', 'OPERATOR_REMOVED'))
        );
    """)

    # The "who's in index X right now" hot query. Active constituents
    # have effective_to IS NULL.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_iic_active
            ON industry_index_constituent (industry_name, ticker_id)
            WHERE effective_to IS NULL;
    """)

    # The "who was in index X on date D" walk. Index on
    # (industry_name, effective_from DESC) lets us scan backwards
    # to find the latest rebalance at-or-before D.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_iic_asof
            ON industry_index_constituent (industry_name, effective_from DESC);
    """)

    op.execute("""
        COMMENT ON COLUMN industry_index_constituent.weight_at_rebalance IS
            'Weight assigned at the rebalance, fixed for this '
            'effective period. Modified Laspeyres formula uses '
            'this weight throughout the period. Mid-cycle '
            'delistings are handled at value-computation time '
            'via drop-and-renormalize, NOT by mutating this row.';
    """)
    op.execute("""
        COMMENT ON COLUMN industry_index_constituent.removed_reason IS
            'Why this constituent''s effective_to was set. '
            'REBALANCE_EXIT = next rebalance picked a different '
            'top-10. DELISTED = ticker was delisted mid-cycle. '
            'OPERATOR_REMOVED = manual intervention.';
    """)

    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON industry_index_constituent
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT ON industry_index_constituent TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS industry_index_constituent;")
