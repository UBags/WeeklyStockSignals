"""ETF classification: 'ETF' bucket + longName column

Small follow-up to 0006_fundamentals_phase1. Two changes, both
additive:

1. Add 'ETF' to the allowed values for
   `instrument_industry.classification_source`. The runner uses this
   value when it classifies a ticker by its instrument_type rather
   than by calling yfinance.

2. Add a nullable `etf_long_name` column to `instrument_industry`.
   For ETFs, this captures the long human-readable name from
   yfinance (e.g. "Nippon India ETF Nifty IT"), which is the ONE
   useful piece of metadata yfinance provides for Indian ETFs.
   Equity rows leave this column NULL.

Why we didn't build a separate `instrument_etf` table: the
yfinance reconnaissance (see scripts/probe_etf_yfinance.py output)
showed that Indian ETF metadata is empty across the board on
yfinance — no category, no fundFamily, no inception date, nothing
useful for analytics. Building a richer schema would mean a lot of
empty columns with no source to populate them. A future migration
will add an instrument_etf table IF and WHEN a real ETF metadata
source becomes available (paid API, NSE filings, etc.). Until
then, the single bucket + longName captures everything we have.

Legacy parity: migrations/legacy/014_etf_classification.sql.

Revision ID: 0007_etf_classification
Revises: 0006_fundamentals_phase1
Create Date: 2026-05-21
"""
from __future__ import annotations

from alembic import op


revision = "0007_etf_classification"
down_revision = "0006_fundamentals_phase1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add 'ETF' to the classification_source CHECK constraint.
    # ALTER CONSTRAINT requires DROP + ADD because Postgres can't
    # modify a CHECK in place. This is safe — no existing row uses
    # 'ETF' since this is the first migration introducing the value.
    op.execute("""
        ALTER TABLE instrument_industry
            DROP CONSTRAINT IF EXISTS instrument_industry_source_chk;
    """)
    op.execute("""
        ALTER TABLE instrument_industry
            ADD CONSTRAINT instrument_industry_source_chk
            CHECK (classification_source IN (
                'YFINANCE', 'GROWW', 'MANUAL_OVERRIDE',
                'NSE_SECTORAL', 'INSTRUMENT_TYPE'
            ));
    """)

    # 2. Add etf_long_name column. Nullable because the vast majority
    # of rows are equities, which never populate this.
    op.execute("""
        ALTER TABLE instrument_industry
            ADD COLUMN IF NOT EXISTS etf_long_name VARCHAR(256);
    """)

    op.execute("""
        COMMENT ON COLUMN instrument_industry.etf_long_name IS
            'For ETFs only: the long human-readable name from '
            'yfinance (e.g. "Nippon India ETF Nifty IT"). NULL '
            'for equities. Captures the one piece of ETF metadata '
            'yfinance reliably provides for Indian markets.';
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE instrument_industry
            DROP COLUMN IF EXISTS etf_long_name;
    """)
    # Restore the previous CHECK constraint.
    op.execute("""
        ALTER TABLE instrument_industry
            DROP CONSTRAINT IF EXISTS instrument_industry_source_chk;
    """)
    op.execute("""
        ALTER TABLE instrument_industry
            ADD CONSTRAINT instrument_industry_source_chk
            CHECK (classification_source IN (
                'YFINANCE', 'GROWW', 'MANUAL_OVERRIDE', 'NSE_SECTORAL'
            ));
    """)
