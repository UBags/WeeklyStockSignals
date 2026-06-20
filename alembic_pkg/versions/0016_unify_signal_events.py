"""Unify signal_events (plural) and signal_event (singular).

Background: signal_events (plural) was created in migration 002
(legacy) as the per-ticker live-signal table; notification_log
references its event_id. Phase 6-1 then added signal_event
(singular, migration 0015) for per-industry and per-ticker
backfilled signals. Two tables tracking the same concept is bad
design — dual writers, inconsistent column names, inconsistent
EXIT_* value spellings, and the GUI / enrichment / scheduler /
notification pipeline each picked one or the other.

This migration consolidates everything into signal_events as the
SINGLE source of truth.

Changes:
  1. Add industry_name (nullable, FK industry_index)
  2. Add regime_before, regime_after (nullable for backward compat
     with rows written before Phase 6-1 design)
  3. Make ticker_id and timeframe nullable
  4. Add CHECK enforcing exactly one of (ticker_id+timeframe) or
     industry_name set per row
  5. Deduplicate existing rows that conflict on the new uniqueness
     constraint (keeping the most recent computed_at per group)
  6. Add unique partial indexes preventing future duplicates
  7. Migrate signal_event rows into signal_events with column +
     value translation:
       trade_date (DATE)   → bar_timestamp (TIMESTAMPTZ at 00:00 UTC)
       event_kind          → signal_type
         'EXIT_BUY'         → 'EXIT_FROM_BUY'
         'EXIT_SELL'        → 'EXIT_FROM_SELL'
       close_at_event      → price_at_signal
  8. Drop signal_event.

After this migration, Phase 6-1 runners must write to signal_events
(not signal_event). See companion code change in
fundamentals_repo.SignalEventRepository.

Revision ID: 0016_unify_signal_events
Revises: 0015_signal_event
Create Date: 2026-06-19
"""
from __future__ import annotations

from alembic import op


revision = "0016_unify_signal_events"
down_revision = "0015_signal_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. Add new nullable columns ----
    op.execute("""
        ALTER TABLE signal_events
            ADD COLUMN industry_name VARCHAR(128),
            ADD COLUMN regime_before SMALLINT,
            ADD COLUMN regime_after  SMALLINT
    """)

    # ---- 2. Foreign key on industry_name ----
    op.execute("""
        ALTER TABLE signal_events
            ADD CONSTRAINT signal_events_industry_name_fkey
            FOREIGN KEY (industry_name)
            REFERENCES industry_index(industry_name)
            ON DELETE CASCADE
    """)

    # ---- 3. Make ticker_id and timeframe nullable ----
    op.execute("ALTER TABLE signal_events ALTER COLUMN ticker_id DROP NOT NULL")
    op.execute("ALTER TABLE signal_events ALTER COLUMN timeframe DROP NOT NULL")

    # ---- 4. CHECK constraint: exactly one of (ticker_id+timeframe) or
    #         industry_name must be set. Matches indicator_state and the
    #         old signal_event constraint.
    op.execute("""
        ALTER TABLE signal_events
            ADD CONSTRAINT signal_events_series_xor_chk
            CHECK (
                (ticker_id IS NOT NULL AND timeframe IS NOT NULL
                 AND industry_name IS NULL)
                OR
                (ticker_id IS NULL AND timeframe IS NULL
                 AND industry_name IS NOT NULL)
            )
    """)

    # Regime sanity check (only when set — old rows lacked these).
    op.execute("""
        ALTER TABLE signal_events
            ADD CONSTRAINT signal_events_regime_before_chk
            CHECK (regime_before IS NULL OR regime_before IN (-1, 0, 1))
    """)
    op.execute("""
        ALTER TABLE signal_events
            ADD CONSTRAINT signal_events_regime_after_chk
            CHECK (regime_after IS NULL OR regime_after IN (-1, 0, 1))
    """)

    # ---- 5. Dedup existing ticker rows. The old schema had NO
    #         uniqueness constraint, so repeat compute passes have
    #         likely accumulated duplicates (the GUI screenshot
    #         showing 4× EXIT_FROM_SELL on one date confirms this).
    #         Keep the LATEST computed_at row per
    #         (ticker_id, timeframe, bar_timestamp, signal_type) group;
    #         the corresponding notification_log rows ride along via
    #         the existing FK cascade.
    op.execute("""
        WITH ranked AS (
            SELECT event_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY ticker_id, timeframe,
                                    bar_timestamp, signal_type
                       ORDER BY computed_at DESC, event_id DESC
                   ) AS rn
            FROM signal_events
            WHERE ticker_id IS NOT NULL
        )
        DELETE FROM signal_events
        WHERE event_id IN (SELECT event_id FROM ranked WHERE rn > 1)
    """)

    # ---- 6. Unique partial indexes ----
    op.execute("""
        CREATE UNIQUE INDEX uq_signal_events_ticker
            ON signal_events (ticker_id, timeframe, bar_timestamp, signal_type)
            WHERE ticker_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_signal_events_industry
            ON signal_events (industry_name, bar_timestamp, signal_type)
            WHERE industry_name IS NOT NULL
    """)

    # Helpful access patterns inherited from the old signal_event
    # table's indexes.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_events_bar_timestamp
            ON signal_events (bar_timestamp DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_events_signal_type_ts
            ON signal_events (signal_type, bar_timestamp DESC)
    """)

    # ---- 7. Migrate signal_event → signal_events ----
    # Cast DATE to TIMESTAMPTZ at 00:00 UTC. Translate EXIT_BUY →
    # EXIT_FROM_BUY, EXIT_SELL → EXIT_FROM_SELL so the value set is
    # uniform with the existing rows. ON CONFLICT DO NOTHING so any
    # already-present rows (extremely unlikely but defensive) don't
    # block the migration.
    op.execute("""
        INSERT INTO signal_events (
            ticker_id, timeframe, industry_name,
            bar_timestamp, signal_type,
            regime_before, regime_after,
            price_at_signal, computed_at
        )
        SELECT
            ticker_id,
            timeframe,
            industry_name,
            (trade_date::timestamp AT TIME ZONE 'UTC') AS bar_timestamp,
            CASE event_kind
                WHEN 'EXIT_BUY'  THEN 'EXIT_FROM_BUY'
                WHEN 'EXIT_SELL' THEN 'EXIT_FROM_SELL'
                ELSE event_kind
            END AS signal_type,
            regime_before,
            regime_after,
            close_at_event,
            computed_at
        FROM signal_event
        ON CONFLICT DO NOTHING
    """)

    # ---- 8. Drop the old singular table ----
    op.execute("DROP TABLE IF EXISTS signal_event")

    op.execute("""
        COMMENT ON TABLE signal_events IS
            'Unified signal-event log (Phase 6-2). One row per fired '
            'signal across either per-ticker series (ticker_id + '
            'timeframe) or per-industry series (industry_name). '
            'Replaces the dual signal_events/signal_event design from '
            'pre-0016.'
    """)


def downgrade() -> None:
    # Best-effort downgrade. Industry rows in signal_events are LOST
    # because the original signal_events had no industry column.
    op.execute("""
        CREATE TABLE IF NOT EXISTS signal_event (
            ticker_id        INTEGER REFERENCES tickers(ticker_id) ON DELETE CASCADE,
            timeframe        VARCHAR(8),
            industry_name    VARCHAR(128) REFERENCES industry_index(industry_name) ON DELETE CASCADE,
            trade_date       DATE NOT NULL,
            event_kind       VARCHAR(16) NOT NULL,
            regime_before    SMALLINT NOT NULL,
            regime_after     SMALLINT NOT NULL,
            close_at_event   DOUBLE PRECISION NOT NULL,
            computed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT signal_event_series_xor_chk CHECK (
                (ticker_id IS NOT NULL AND timeframe IS NOT NULL AND industry_name IS NULL)
                OR
                (ticker_id IS NULL AND timeframe IS NULL AND industry_name IS NOT NULL)
            ),
            CONSTRAINT signal_event_kind_chk CHECK (
                event_kind IN ('BUY', 'SELL', 'EXIT_BUY', 'EXIT_SELL')
            )
        )
    """)
    op.execute("DROP INDEX IF EXISTS idx_signal_events_signal_type_ts")
    op.execute("DROP INDEX IF EXISTS idx_signal_events_bar_timestamp")
    op.execute("DROP INDEX IF EXISTS uq_signal_events_industry")
    op.execute("DROP INDEX IF EXISTS uq_signal_events_ticker")
    op.execute("ALTER TABLE signal_events DROP CONSTRAINT IF EXISTS signal_events_regime_after_chk")
    op.execute("ALTER TABLE signal_events DROP CONSTRAINT IF EXISTS signal_events_regime_before_chk")
    op.execute("ALTER TABLE signal_events DROP CONSTRAINT IF EXISTS signal_events_series_xor_chk")
    op.execute("DELETE FROM signal_events WHERE industry_name IS NOT NULL")
    op.execute("ALTER TABLE signal_events ALTER COLUMN timeframe SET NOT NULL")
    op.execute("ALTER TABLE signal_events ALTER COLUMN ticker_id SET NOT NULL")
    op.execute("ALTER TABLE signal_events DROP CONSTRAINT IF EXISTS signal_events_industry_name_fkey")
    op.execute("ALTER TABLE signal_events DROP COLUMN IF EXISTS regime_after")
    op.execute("ALTER TABLE signal_events DROP COLUMN IF EXISTS regime_before")
    op.execute("ALTER TABLE signal_events DROP COLUMN IF EXISTS industry_name")
