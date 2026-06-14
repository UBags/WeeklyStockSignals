"""industry_index table (Phase 4B-1).

One row per industry that qualifies for indexing (≥ min_constituents
active classified EQ tickers). Auto-discovered from instrument_industry
by the build-indices runner (Phase 4B-2). The primary key is
`industry_name` itself — simple, debuggable joins, with the trade-off
that yfinance industry-name drift creates parallel indices rather
than continuous ones (see docs/FUNDAMENTALS_PHASES_1_TO_8.md §20).

What this migration adds:
  - industry_index — the index definition table.

Columns:
  - industry_name: the canonical name from yfinance. PK.
  - sector_name: the broader yfinance sector. Denormalized here
    for query convenience; derived from the constituents' rows
    in instrument_industry at index-creation time. If sector
    differs across constituents (rare but possible after
    classification refreshes), we store the modal value.
  - methodology: 'MODIFIED_LASPEYRES' is the only value currently.
    Stored as a column so future indices could use different
    methodologies (equal-weighted, free-float-weighted, etc.)
    without a schema migration.
  - base_value: the index value at base_date. Default 1000.0 per
    NIFTY convention.
  - base_date: first rebalance date for this index. Index values
    for dates before this are not computed (per the §20 decision).
  - rebalance_schedule: 'SEMIANNUAL_APR_OCT' is the only value
    currently. Same forward-compat reasoning as `methodology`.
  - min_constituents: the threshold this index was created with.
    Stored per-index so a future change to the global default
    (e.g. 10 → 12) doesn't retroactively invalidate existing
    indices.
  - status: 'ACTIVE' or 'RETIRED'. RETIRED indices keep their
    historical values but get no new rebalances or value rows.

What this migration deliberately does NOT add:
  - A `created_at` column. The base_date plus the
    fundamentals_run audit trail suffice for provenance.
  - Foreign keys to constituent ticker_ids. Those live on
    industry_index_constituent (migration 0012) where they belong.

Legacy parity: migrations/legacy/018_industry_index.sql

Revision ID: 0011_industry_index
Revises: 0010_daily_market_cap
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op


revision = "0011_industry_index"
down_revision = "0010_daily_market_cap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS industry_index (
            industry_name        VARCHAR(128)     NOT NULL,
            sector_name          VARCHAR(128)     NOT NULL,
            methodology          VARCHAR(32)      NOT NULL DEFAULT 'MODIFIED_LASPEYRES',
            base_value           DOUBLE PRECISION NOT NULL DEFAULT 1000.0,
            base_date            DATE             NOT NULL,
            rebalance_schedule   VARCHAR(32)      NOT NULL DEFAULT 'SEMIANNUAL_APR_OCT',
            min_constituents     INTEGER          NOT NULL DEFAULT 10,
            status               VARCHAR(16)      NOT NULL DEFAULT 'ACTIVE',
            notes                TEXT,

            PRIMARY KEY (industry_name),

            CONSTRAINT industry_index_methodology_chk
                CHECK (methodology IN ('MODIFIED_LASPEYRES',
                                       'EQUAL_WEIGHTED',
                                       'FREE_FLOAT_WEIGHTED')),
            CONSTRAINT industry_index_schedule_chk
                CHECK (rebalance_schedule IN ('SEMIANNUAL_APR_OCT',
                                              'QUARTERLY',
                                              'ANNUAL_APR',
                                              'MANUAL')),
            CONSTRAINT industry_index_status_chk
                CHECK (status IN ('ACTIVE', 'RETIRED')),
            CONSTRAINT industry_index_base_value_chk
                CHECK (base_value > 0),
            CONSTRAINT industry_index_min_constituents_chk
                CHECK (min_constituents >= 1)
        );
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_industry_index_active
            ON industry_index (status, industry_name)
            WHERE status = 'ACTIVE';
    """)

    op.execute("""
        COMMENT ON TABLE industry_index IS
            'One row per indexed yfinance industry. industry_name as '
            'PK is the operator''s choice (see §20 design doc); '
            'yfinance name drift would create parallel rather than '
            'continuous index history.';
    """)
    op.execute("""
        COMMENT ON COLUMN industry_index.base_date IS
            'First rebalance date. Index values pre-base_date are '
            'NOT computed (no weights exist before first rebalance).';
    """)
    op.execute("""
        COMMENT ON COLUMN industry_index.min_constituents IS
            'Threshold this index was created with. Stored per-index '
            'so a global default change does not retroactively '
            'invalidate existing indices.';
    """)

    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_etl_user') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON industry_index
                    TO trip_etl_user;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trip_gui_user') THEN
                GRANT SELECT ON industry_index TO trip_gui_user;
            END IF;
        END
        $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS industry_index;")
