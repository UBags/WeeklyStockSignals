---
-- Migration 030 — drop legacy signal_events table.
--
-- Bit-equivalent parallel of
-- alembic_pkg/versions/0030_drop_legacy_signal_tables.py.
--
-- The legacy-parity regression test (test_legacy_migrator.py) is
-- currently skipped due to post-0018 drift, so this file is archival.
---

BEGIN;

-- Drop FK explicitly.
ALTER TABLE public.notification_log
    DROP CONSTRAINT IF EXISTS notification_log_event_id_fkey;

-- Drop signal_events. CASCADE handles dependent objects.
DROP TABLE IF EXISTS public.signal_events CASCADE;

COMMIT;
