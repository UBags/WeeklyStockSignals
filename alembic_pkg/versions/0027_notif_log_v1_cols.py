"""Migration 0027 — denormalize signal identity into notification_log.

V1 cutover Step 8.

Adds 5 typed columns to `notification_log` carrying the signal
identity that was previously reachable only via the `event_id` FK
to `signal_events`:

  - signal_ticker_id      INTEGER
  - signal_timeframe      VARCHAR(8)
  - signal_bar_timestamp  TIMESTAMPTZ
  - signal_type           VARCHAR(16)  (CHECK constrained)
  - signal_price          DOUBLE PRECISION

Plus a CHECK constraint pinning `signal_type` to the V1 signal-kind
set (mirroring the constraint on `current_signals` / `historical_signals`
from migration 0020).

Backfill: existing rows have `event_id` set — UPDATE FROM signal_events
to populate the new columns. Rows with NULL event_id (none currently,
but the SQL is null-safe) are skipped.

**Additive only — does NOT drop event_id or the FK.** The legacy
`SubscribersRepository.record_notification(... event_id ...)` writer
and `trip_ema/notifier_worker.py` still use those. Step 14 demolishes
them together along with the now-redundant columns.

Legacy parity: migrations/legacy/031_notif_log_v1_cols.sql.

Revision ID: 0027_notif_log_v1_cols
Revises:     0026_v1_industry_enrich_phase
Create Date: 2026-06-27
"""
from __future__ import annotations

from alembic import op


revision = "0027_notif_log_v1_cols"
down_revision = "0026_v1_industry_enrich_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ----- ADD the 5 new columns, all nullable for backfill -----
    op.execute("""
        ALTER TABLE notification_log
            ADD COLUMN IF NOT EXISTS signal_ticker_id     INTEGER,
            ADD COLUMN IF NOT EXISTS signal_timeframe     VARCHAR(8),
            ADD COLUMN IF NOT EXISTS signal_bar_timestamp TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS signal_type          VARCHAR(16),
            ADD COLUMN IF NOT EXISTS signal_price         DOUBLE PRECISION
    """)

    # ----- Relax event_id to nullable -----
    # The V1 writer (NotificationLogRepository) doesn't populate
    # event_id — it lives only in the new typed columns. Legacy
    # writers (SubscribersRepository.record_notification +
    # notifier_worker.py) still populate it. Both shapes coexist
    # until Step 14 drops the column entirely.
    op.execute("""
        ALTER TABLE notification_log
            ALTER COLUMN event_id DROP NOT NULL
    """)

    # ----- CHECK constraint on signal_type -----
    # NULL is allowed during the transition window — legacy rows
    # without backfill carry NULL. Step 14 will tighten this to
    # NOT NULL after the legacy event_id is dropped.
    op.execute("""
        ALTER TABLE notification_log
            ADD CONSTRAINT notification_log_signal_type_chk
                CHECK (signal_type IS NULL OR signal_type IN (
                    'BUY', 'SELL', 'EXIT_FROM_BUY', 'EXIT_FROM_SELL'
                ))
    """)

    # ----- Backfill from existing FK join -----
    # signal_events has the V1-shape column names already (the A1+A2+A3
    # demolition migrated it). Direct column → column copy; no casts.
    op.execute("""
        UPDATE notification_log nl
           SET signal_ticker_id     = se.ticker_id,
               signal_timeframe     = se.timeframe,
               signal_bar_timestamp = se.bar_timestamp,
               signal_type          = se.signal_type,
               signal_price         = se.price_at_signal
          FROM signal_events se
         WHERE nl.event_id = se.event_id
           AND nl.signal_ticker_id IS NULL
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE notification_log
            DROP CONSTRAINT IF EXISTS
                notification_log_signal_type_chk
    """)
    op.execute("""
        ALTER TABLE notification_log
            DROP COLUMN IF EXISTS signal_price,
            DROP COLUMN IF EXISTS signal_type,
            DROP COLUMN IF EXISTS signal_bar_timestamp,
            DROP COLUMN IF EXISTS signal_timeframe,
            DROP COLUMN IF EXISTS signal_ticker_id
    """)
    # Restore event_id NOT NULL — but only if no V1-shape rows exist.
    # If any rows have NULL event_id (i.e. V1 writes already landed),
    # the downgrade will fail. The operator must clean those up first.
    op.execute("""
        ALTER TABLE notification_log
            ALTER COLUMN event_id SET NOT NULL
    """)
