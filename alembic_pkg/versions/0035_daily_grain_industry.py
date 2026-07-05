"""Daily-grain industry_indicator_state + industry_indicator_enrichment.

Industry-side mirror of migration 0034 (which did the stock tables).
Moves the two INDUSTRY indicator tables from one-row-per-weekly-bar to
one-row-per-trading-day, where each day's row holds the WEEKLY-chart
indicator values as they stood at the close of that day (the still-
forming weekly bar of the industry index, sampled daily).

What changes on both industry tables:
  - Adds `date DATE NOT NULL` — the IST trading date the row describes.
  - PK moves from (industry_name, timeframe, bar_timestamp)
             to   (industry_name, timeframe, date).
  - `bar_timestamp TIMESTAMPTZ NOT NULL` is RETAINED as a plain
    attribute: the IST-Monday week-anchor of the week `date` falls in.
    No longer unique per row (up to ~5 daily rows share one anchor).
  - The enrichment→state composite FK is dropped and recreated against
    the new (industry_name, timeframe, date) parent key, ON DELETE
    CASCADE.
  - New indexes on (industry_name, date DESC) and (date DESC); the old
    (…, bar_timestamp DESC) week-anchor indexes are kept.

Data handling: both tables are FULLY DERIVED from industry_index_value
via the V1 recompute path, so upgrade/downgrade TRUNCATE both (child
first) and the operator re-runs the recompute at the new grain.

Revision ID: 0035_daily_grain_industry
Revises:     0034_daily_grain_indicator_state
Create Date: architecture/clean-pipeline-v1
"""
from __future__ import annotations

from alembic import op

revision = "0035_daily_grain_industry"
down_revision = "0034_daily_grain_indicator_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0. Clear both (child first). Fully derived; recompute repopulates
    #    at daily grain, and TRUNCATE lets us add a NOT NULL PK-member
    #    `date` column without a throwaway backfill.
    op.execute(
        "TRUNCATE TABLE industry_indicator_enrichment, "
        "industry_indicator_state;"
    )

    # 1. Drop the child→parent composite FK first.
    op.execute("""
        ALTER TABLE industry_indicator_enrichment
            DROP CONSTRAINT IF EXISTS
                industry_indicator_enrichment_state_fk;
    """)

    # 2. industry_indicator_state: add `date`, swap PK.
    op.execute("""
        ALTER TABLE industry_indicator_state
            ADD COLUMN IF NOT EXISTS date DATE NOT NULL;
    """)
    op.execute("""
        ALTER TABLE industry_indicator_state
            DROP CONSTRAINT industry_indicator_state_pkey;
    """)
    op.execute("""
        ALTER TABLE industry_indicator_state
            ADD CONSTRAINT industry_indicator_state_pkey
            PRIMARY KEY (industry_name, timeframe, date);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS
            idx_industry_indicator_state_industry_date
            ON industry_indicator_state (industry_name, date DESC);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_industry_indicator_state_date
            ON industry_indicator_state (date DESC);
    """)
    op.execute("""
        COMMENT ON TABLE industry_indicator_state IS
            'V1 per-industry EMA/ATR/regime trajectory at DAILY grain. '
            'PK (industry_name, timeframe, date). Each row is the '
            'weekly-chart indicator state of the industry index as of '
            'the close of `date`; bar_timestamp is the IST-Monday '
            'week-anchor (retained, non-unique). Fully derived from '
            'industry_index_value via the V1 recompute path.';
    """)
    op.execute("""
        COMMENT ON COLUMN industry_indicator_state.date IS
            'IST trading date this row describes. PK member.';
    """)
    op.execute("""
        COMMENT ON COLUMN industry_indicator_state.bar_timestamp IS
            'IST-Monday 00:00 (stored UTC) week-anchor of the week '
            'containing `date`. Non-unique attribute.';
    """)

    # 3. industry_indicator_enrichment: add `date`, swap PK, recreate FK
    #    against the new (industry_name, timeframe, date) parent key.
    op.execute("""
        ALTER TABLE industry_indicator_enrichment
            ADD COLUMN IF NOT EXISTS date DATE NOT NULL;
    """)
    op.execute("""
        ALTER TABLE industry_indicator_enrichment
            DROP CONSTRAINT industry_indicator_enrichment_pkey;
    """)
    op.execute("""
        ALTER TABLE industry_indicator_enrichment
            ADD CONSTRAINT industry_indicator_enrichment_pkey
            PRIMARY KEY (industry_name, timeframe, date);
    """)
    op.execute("""
        ALTER TABLE industry_indicator_enrichment
            ADD CONSTRAINT industry_indicator_enrichment_state_fk
            FOREIGN KEY (industry_name, timeframe, date)
            REFERENCES industry_indicator_state
                (industry_name, timeframe, date)
            ON DELETE CASCADE;
    """)
    op.execute("""
        COMMENT ON TABLE industry_indicator_enrichment IS
            'V1 per-industry enrichment fields at DAILY grain. 1:1 with '
            'industry_indicator_state via composite (industry_name, '
            'timeframe, date) FK, ON DELETE CASCADE. Inter-bar '
            'quantities remain WEEK-counted.';
    """)
    op.execute("""
        COMMENT ON COLUMN industry_indicator_enrichment.date IS
            'IST trading date this row describes. PK member; matches '
            'the parent industry_indicator_state row.';
    """)


def downgrade() -> None:
    # Symmetric restore to weekly grain; contents are regenerable.
    op.execute(
        "TRUNCATE TABLE industry_indicator_enrichment, "
        "industry_indicator_state;"
    )
    op.execute("""
        ALTER TABLE industry_indicator_enrichment
            DROP CONSTRAINT IF EXISTS
                industry_indicator_enrichment_state_fk;
    """)

    # enrichment: PK back to bar_timestamp, drop `date`
    op.execute("""
        ALTER TABLE industry_indicator_enrichment
            DROP CONSTRAINT industry_indicator_enrichment_pkey;
    """)
    op.execute("""
        ALTER TABLE industry_indicator_enrichment
            ADD CONSTRAINT industry_indicator_enrichment_pkey
            PRIMARY KEY (industry_name, timeframe, bar_timestamp);
    """)
    op.execute("""
        ALTER TABLE industry_indicator_enrichment
            DROP COLUMN IF EXISTS date;
    """)

    # state: drop daily indexes, PK back to bar_timestamp, drop `date`
    op.execute(
        "DROP INDEX IF EXISTS "
        "idx_industry_indicator_state_industry_date;")
    op.execute("DROP INDEX IF EXISTS idx_industry_indicator_state_date;")
    op.execute("""
        ALTER TABLE industry_indicator_state
            DROP CONSTRAINT industry_indicator_state_pkey;
    """)
    op.execute("""
        ALTER TABLE industry_indicator_state
            ADD CONSTRAINT industry_indicator_state_pkey
            PRIMARY KEY (industry_name, timeframe, bar_timestamp);
    """)
    op.execute("""
        ALTER TABLE industry_indicator_state
            DROP COLUMN IF EXISTS date;
    """)

    # recreate the weekly FK
    op.execute("""
        ALTER TABLE industry_indicator_enrichment
            ADD CONSTRAINT industry_indicator_enrichment_state_fk
            FOREIGN KEY (industry_name, timeframe, bar_timestamp)
            REFERENCES industry_indicator_state
                (industry_name, timeframe, bar_timestamp)
            ON DELETE CASCADE;
    """)
