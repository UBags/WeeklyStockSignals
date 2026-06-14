"""industry_index_value table (Phase 4B-1).

Daily index values produced by the compute layer (Phase 4B-2). One
row per (index, trade_date). Phase 5+ indicator stack reads from
this table.

Columns:
  - industry_name: FK to industry_index.
  - trade_date: the trading day this value is for.
  - value: the modified Laspeyres index value:
      value(t) = base_value × Σ_i [
          weight_i × close_i(t) / close_i(t_rebalance)
      ]
    where the sum is over active constituents on date t (mid-cycle
    delistings dropped and weights effectively re-normalized at
    compute time).
  - constituents_used: how many of the rebalance constituents
    contributed to today's value. Equals min_constituents (typically
    10) on a clean day; lower if any constituent was dropped due to
    delisting or missing mcap.
  - constituents_missing: count of constituents on the books for
    this rebalance period that did NOT contribute. Sum of
    used + missing = total constituents in the active rebalance
    period.
  - renormalized: TRUE if any constituent was missing and weights
    were re-normalized across the remaining ones. A flag rather
    than a fraction because Phase 5+ analytics may want to ignore
    days where the index was "incomplete".
  - effective_rebalance_date: which rebalance produced the weights
    used today. Lets analytics correlate value movements with
    constituent changes.
  - computed_at: when the row was written.

PK is (industry_name, trade_date) — natural for daily series, same
shape as daily_market_cap.

Indexes:
  - idx_industry_index_value_date for the cross-industry analytics
    query "show me all industries' values on date D".
  - The PK itself covers the per-industry time series read.

Legacy parity: migrations/legacy/020_industry_index_value.sql

Revision ID: 0013_industry_index_value
Revises: 0012_industry_index_constituent
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


revision = "0013_industry_index_value"
down_revision = "0012_industry_index_constituent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS industry_index_value (
            industry_name             VARCHAR(128)     NOT NULL
                                                       REFERENCES industry_index(industry_name) ON DELETE CASCADE,
            trade_date                DATE             NOT NULL,
            value                     DOUBLE PRECISION NOT NULL,
            constituents_used         INTEGER          NOT NULL,
            constituents_missing      INTEGER          NOT NULL DEFAULT 0,
            renormalized              BOOLEAN          NOT NULL DEFAULT FALSE,
            effective_rebalance_date  DATE             NOT NULL,
            computed_at               TIMESTAMPTZ      NOT NULL DEFAULT NOW(),

            PRIMARY KEY (industry_name, trade_date),

            CONSTRAINT iiv_value_chk CHECK (value > 0),
            CONSTRAINT iiv_used_chk CHECK (constituents_used >= 0),
            CONSTRAINT iiv_missing_chk CHECK (constituents_missing >= 0)
        );
    """)

    # Cross-industry-on-a-date queries — useful for "compare today's
    # IT vs Bank sector movement".
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_industry_index_value_date
            ON industry_index_value (trade_date DESC);
    """)

    op.execute("""
        COMMENT ON TABLE industry_index_value IS
            'Daily index values from the modified Laspeyres compute '
            '(Phase 4B-2). PK is (industry_name, trade_date). Index '
            'values pre-base_date are not stored (see §20).';
    """)
    op.execute("""
        COMMENT ON COLUMN industry_index_value.renormalized IS
            'TRUE if any constituent was missing for this date and '
            'weights were re-normalized across the remaining ones. '
            'Phase 5+ analytics may want to filter out renormalized '
            'days for clean comparisons.';
    """)
    op.execute("""
        COMMENT ON COLUMN industry_index_value.effective_rebalance_date IS
            'Which rebalance''s constituent weights were used to '
            'compute this value. Lets analytics correlate value '
            'movements with constituent changes.';
    """)

    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON industry_index_value
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT ON industry_index_value TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS industry_index_value;")
