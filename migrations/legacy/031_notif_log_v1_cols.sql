-- Legacy SQL parity for alembic migration 0027.
-- V1 cutover Step 8: denormalize signal identity into notification_log.
--
-- Mirrors alembic_pkg/versions/0027_notif_log_v1_cols.py exactly.
-- For deploy environments that use the raw-SQL migrator instead of
-- alembic.

BEGIN;

ALTER TABLE notification_log
    ADD COLUMN IF NOT EXISTS signal_ticker_id     INTEGER,
    ADD COLUMN IF NOT EXISTS signal_timeframe     VARCHAR(8),
    ADD COLUMN IF NOT EXISTS signal_bar_timestamp TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS signal_type          VARCHAR(16),
    ADD COLUMN IF NOT EXISTS signal_price         DOUBLE PRECISION;

-- Relax event_id to nullable: V1 writes don't populate it; legacy
-- writes still do. Step 14 drops the column entirely.
ALTER TABLE notification_log
    ALTER COLUMN event_id DROP NOT NULL;

ALTER TABLE notification_log
    ADD CONSTRAINT notification_log_signal_type_chk
        CHECK (signal_type IS NULL OR signal_type IN (
            'BUY', 'SELL', 'EXIT_FROM_BUY', 'EXIT_FROM_SELL'
        ));

UPDATE notification_log nl
   SET signal_ticker_id     = se.ticker_id,
       signal_timeframe     = se.timeframe,
       signal_bar_timestamp = se.bar_timestamp,
       signal_type          = se.signal_type,
       signal_price         = se.price_at_signal
  FROM signal_events se
 WHERE nl.event_id = se.event_id
   AND nl.signal_ticker_id IS NULL;

COMMIT;
