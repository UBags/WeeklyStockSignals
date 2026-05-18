-- 008_drop_notification_log_dedup_index.sql
-- Phase 9 C1: drop the misleading UNIQUE index on notification_log.
--
-- The index was created in migration 003 with the intent of enforcing
-- per-(subscriber, event) deduplication at the DB level:
--
--     CREATE UNIQUE INDEX idx_notification_log_dedup
--         ON notification_log (subscriber_id, event_id, last_sent_at);
--
-- But because `last_sent_at` is part of the unique tuple AND defaults to
-- NOW() on insert, every insert gets a microsecond-distinct timestamp,
-- so the constraint never actually triggers. It enforces nothing.
--
-- Real dedup happens at the application layer:
--   1. `find_pending_for_subscriber` (in signals_repo.py) counts existing
--      rows for (subscriber_id, event_id) and caps at 3 per event.
--   2. The Redis queue's Lua-atomic enqueue with the dedup hash prevents
--      double-enqueue of the same (subscriber, event) while one is in
--      flight.
--
-- The misleading index is dropped here. The lookup index
-- `idx_notification_log_subscriber_event` (created in the same migration
-- 003) remains in place — it powers the count query that enforces the
-- 3-send cap.

DROP INDEX IF EXISTS idx_notification_log_dedup;

INSERT INTO schema_migrations (version) VALUES ('008') ON CONFLICT DO NOTHING;
