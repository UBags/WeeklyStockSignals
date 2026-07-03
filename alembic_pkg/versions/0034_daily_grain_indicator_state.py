"""Daily-grain stock_indicator_state + stock_indicator_enrichment.

Moves the two STOCK indicator tables from one-row-per-weekly-bar to
one-row-per-trading-day, where each day's row holds the WEEKLY-chart
indicator values as they stood at the close of that day (the still-
forming weekly bar sampled daily).

What changes on `stock_indicator_state` and `stock_indicator_enrichment`:

  - Adds `date DATE NOT NULL` — the IST trading date the row describes.
  - PK moves from (ticker_id, timeframe, bar_timestamp)
             to   (ticker_id, timeframe, date).
  - `bar_timestamp TIMESTAMPTZ NOT NULL` is RETAINED as a plain
    attribute: the IST-Monday week-anchor of the week `date` falls in.
    It is no longer unique per row (up to ~5 daily rows share one
    week-anchor).
  - The enrichment→state composite FK is dropped and recreated against
    the new (ticker_id, timeframe, date) parent key, ON DELETE CASCADE.
  - New indexes on (ticker_id, date DESC) and (date DESC) for the
    per-ticker-latest and global-recent reads that now key on `date`.
    The old (…, bar_timestamp DESC) indexes are kept for week-grouped
    scans (e.g. market_overview / lead-lag joins on the week anchor).

Why the STOCK tables only:
  The INDUSTRY tables (industry_indicator_state / _enrichment) stay
  weekly for now — no consumer needs intra-week industry granularity
  yet, and keeping them weekly avoids a larger blast radius. Their
  schema is untouched by this migration.

Data handling (upgrade):
  Both tables are FULLY DERIVED — every row is reproducible from
  ohlcv_bars via the V1 recompute path. There is no meaningful way to
  promote an existing weekly row to daily grain in-place (the intra-
  week days were never computed). So the upgrade TRUNCATEs both tables
  before the PK swap, then the operator re-runs the recompute
  (populate_stock_indicator_state_for_exchange / _enrichment_) to
  repopulate at daily grain. TRUNCATE also sidesteps the NOT-NULL-PK-
  member bootstrap problem (a fresh `date` column can't be NOT NULL on
  a populated table without a backfill we'd immediately discard).

  Order matters: enrichment is truncated first (it's the FK child),
  then state. On the state side we CASCADE anyway, but truncating the
  child first keeps the intent explicit.

Data handling (downgrade):
  Symmetric and equally derived — TRUNCATE both, restore the weekly PK,
  drop `date`, recreate the weekly FK. Operator re-runs the (weekly)
  recompute afterward. The `date` data is not preserved because it is
  regenerable; this is an intentional, documented irreversibility of
  the row *contents* (not the schema).

Revision ID: 0034_daily_grain_indicator_state
Revises:     0033_current_stage
Create Date: architecture/clean-pipeline-v1
"""
from __future__ import annotations

from alembic import op


revision = "0034_daily_grain_indicator_state"
down_revision = "0033_current_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -----------------------------------------------------------------
    # 0. Clear both tables. Enrichment (FK child) first, then state.
    #    Both are fully derived; the recompute path repopulates at the
    #    new daily grain. TRUNCATE also lets us add a NOT NULL `date`
    #    column and make it a PK member without a throwaway backfill.
    # -----------------------------------------------------------------
    # Truncate BOTH in one statement: Postgres refuses to TRUNCATE a
    # table still referenced by a FK unless the referencing table is
    # truncated in the same command (or CASCADE is used). Listing both
    # satisfies that rule explicitly.
    op.execute(
        "TRUNCATE TABLE stock_indicator_enrichment, stock_indicator_state;"
    )

    # -----------------------------------------------------------------
    # 1. Drop the child→parent composite FK. It references the parent
    #    PK we are about to change, so it must go first.
    # -----------------------------------------------------------------
    op.execute("""
        ALTER TABLE stock_indicator_enrichment
            DROP CONSTRAINT IF EXISTS stock_indicator_enrichment_state_fk;
    """)

    # -----------------------------------------------------------------
    # 2. stock_indicator_state: add `date`, swap PK.
    # -----------------------------------------------------------------
    op.execute("""
        ALTER TABLE stock_indicator_state
            ADD COLUMN IF NOT EXISTS date DATE NOT NULL;
    """)
    op.execute("""
        ALTER TABLE stock_indicator_state
            DROP CONSTRAINT stock_indicator_state_pkey;
    """)
    op.execute("""
        ALTER TABLE stock_indicator_state
            ADD CONSTRAINT stock_indicator_state_pkey
            PRIMARY KEY (ticker_id, timeframe, date);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_stock_indicator_state_ticker_date
            ON stock_indicator_state (ticker_id, date DESC);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_stock_indicator_state_date
            ON stock_indicator_state (date DESC);
    """)
    op.execute("""
        COMMENT ON TABLE stock_indicator_state IS
            'V1 per-ticker EMA/ATR/regime trajectory at DAILY grain. '
            'PK (ticker_id, timeframe, date). Each row is the weekly-'
            'chart indicator state as of the close of `date`; '
            'bar_timestamp is the IST-Monday week-anchor of that '
            'week (retained, non-unique). Fully derived; regenerated '
            'via the V1 recompute path.';
    """)
    op.execute("""
        COMMENT ON COLUMN stock_indicator_state.date IS
            'IST trading date this row describes. PK member.';
    """)
    op.execute("""
        COMMENT ON COLUMN stock_indicator_state.bar_timestamp IS
            'IST-Monday 00:00 (stored UTC) week-anchor of the week '
            'containing `date`. Non-unique attribute (up to ~5 daily '
            'rows share one anchor).';
    """)

    # -----------------------------------------------------------------
    # 3. stock_indicator_enrichment: add `date`, swap PK, recreate FK
    #    against the new (ticker_id, timeframe, date) parent key.
    # -----------------------------------------------------------------
    op.execute("""
        ALTER TABLE stock_indicator_enrichment
            ADD COLUMN IF NOT EXISTS date DATE NOT NULL;
    """)
    op.execute("""
        ALTER TABLE stock_indicator_enrichment
            DROP CONSTRAINT stock_indicator_enrichment_pkey;
    """)
    op.execute("""
        ALTER TABLE stock_indicator_enrichment
            ADD CONSTRAINT stock_indicator_enrichment_pkey
            PRIMARY KEY (ticker_id, timeframe, date);
    """)
    op.execute("""
        ALTER TABLE stock_indicator_enrichment
            ADD CONSTRAINT stock_indicator_enrichment_state_fk
            FOREIGN KEY (ticker_id, timeframe, date)
            REFERENCES stock_indicator_state
                (ticker_id, timeframe, date)
            ON DELETE CASCADE;
    """)
    op.execute("""
        COMMENT ON TABLE stock_indicator_enrichment IS
            'V1 per-ticker enrichment fields at DAILY grain. 1:1 with '
            'stock_indicator_state via composite (ticker_id, timeframe, '
            'date) FK, ON DELETE CASCADE. Inter-bar quantities '
            '(bars_since_*_turn, pct_change_since_*_turn, turn flags) '
            'remain WEEK-counted: the forming weekly bar''s predecessor '
            'is the last completed week.';
    """)
    op.execute("""
        COMMENT ON COLUMN stock_indicator_enrichment.date IS
            'IST trading date this row describes. PK member; matches '
            'the parent stock_indicator_state row.';
    """)


def downgrade() -> None:
    # Symmetric restore to weekly grain. Contents are regenerable, so
    # we TRUNCATE rather than attempt to collapse daily rows back to
    # one-per-week. Operator re-runs the weekly recompute afterward.
    op.execute(
        "TRUNCATE TABLE stock_indicator_enrichment, stock_indicator_state;"
    )

    # Drop the daily FK first.
    op.execute("""
        ALTER TABLE stock_indicator_enrichment
            DROP CONSTRAINT IF EXISTS stock_indicator_enrichment_state_fk;
    """)

    # ---- enrichment: PK back to bar_timestamp, drop `date` ----
    op.execute("""
        ALTER TABLE stock_indicator_enrichment
            DROP CONSTRAINT stock_indicator_enrichment_pkey;
    """)
    op.execute("""
        ALTER TABLE stock_indicator_enrichment
            ADD CONSTRAINT stock_indicator_enrichment_pkey
            PRIMARY KEY (ticker_id, timeframe, bar_timestamp);
    """)
    op.execute("""
        ALTER TABLE stock_indicator_enrichment
            DROP COLUMN IF EXISTS date;
    """)

    # ---- state: drop daily indexes, PK back to bar_timestamp, drop `date` ----
    op.execute("DROP INDEX IF EXISTS idx_stock_indicator_state_ticker_date;")
    op.execute("DROP INDEX IF EXISTS idx_stock_indicator_state_date;")
    op.execute("""
        ALTER TABLE stock_indicator_state
            DROP CONSTRAINT stock_indicator_state_pkey;
    """)
    op.execute("""
        ALTER TABLE stock_indicator_state
            ADD CONSTRAINT stock_indicator_state_pkey
            PRIMARY KEY (ticker_id, timeframe, bar_timestamp);
    """)
    op.execute("""
        ALTER TABLE stock_indicator_state
            DROP COLUMN IF EXISTS date;
    """)

    # ---- recreate the weekly FK on enrichment ----
    op.execute("""
        ALTER TABLE stock_indicator_enrichment
            ADD CONSTRAINT stock_indicator_enrichment_state_fk
            FOREIGN KEY (ticker_id, timeframe, bar_timestamp)
            REFERENCES stock_indicator_state
                (ticker_id, timeframe, bar_timestamp)
            ON DELETE CASCADE;
    """)

    # ---- restore weekly table comments ----
    op.execute("""
        COMMENT ON TABLE stock_indicator_state IS
            'V1 per-ticker EMA/ATR/regime trajectory. PK (ticker_id, '
            'timeframe, bar_timestamp). Replaces ticker rows of the '
            'polymorphic indicator_state. Upserted incrementally each '
            'cycle (last N weeks) or fully via Step 17 regenerate CLI.';
    """)
    op.execute("""
        COMMENT ON TABLE stock_indicator_enrichment IS
            'V1 per-ticker enrichment fields (band-pierce flags, '
            'distance metrics, bars-since-turn, pct-change-since-turn, '
            'pattern labels, per-EMA turn flags). 1:1 with '
            'stock_indicator_state via composite FK with ON DELETE '
            'CASCADE. Separated from core so the enrichment set can '
            'evolve without touching the stable core schema.';
    """)
