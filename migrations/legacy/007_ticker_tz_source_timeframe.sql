-- 007_ticker_tz_source_timeframe.sql
-- Phase 8 B4: per-ticker timezone and source timeframe.
--
-- `tz` is the exchange's local timezone (IANA name, e.g. 'Asia/Kolkata').
--    Used to compute "week start" for aggregation. Different markets'
--    weeks start at different UTC instants (Monday 00:00 IST is Sunday
--    18:30 UTC); we honor each exchange's local Monday.
--
-- `source_timeframe` is the resolution at which the ETL writes bars
--    for this ticker — typically smaller than the engine's target
--    timeframe so the engine can aggregate intra-week.
--
-- Existing rows default to 'UTC' / '1W' so they continue working as
-- they did before (preserves test fixtures and any seeded data).

ALTER TABLE tickers
    ADD COLUMN IF NOT EXISTS tz TEXT NOT NULL DEFAULT 'UTC';

ALTER TABLE tickers
    ADD COLUMN IF NOT EXISTS source_timeframe TEXT NOT NULL DEFAULT '1W';

-- Validate source_timeframe values are sensible. Constraint is a
-- soft guarantee; the application layer enforces the allowed set
-- via the Timeframe enum.
ALTER TABLE tickers
    ADD CONSTRAINT tickers_source_timeframe_chk
    CHECK (source_timeframe IN ('15m', '1h', '2h', '1D', '1W'));

INSERT INTO schema_migrations (version) VALUES ('007') ON CONFLICT DO NOTHING;
