-- 006_bars_updated_at.sql
-- Track when each bar was last written/revised. The ComputeScheduler uses
-- this to detect historical revisions — if the ETL rewrites a bar from a
-- prior week (e.g., due to a corporate action or exchange data correction),
-- the engine needs to re-prime from scratch for that ticker rather than
-- silently miss the change.

ALTER TABLE ohlcv_bars
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Trigger to auto-update on UPDATE.
CREATE OR REPLACE FUNCTION _set_ohlcv_bars_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ohlcv_bars_updated_at ON ohlcv_bars;
CREATE TRIGGER trg_ohlcv_bars_updated_at
    BEFORE UPDATE ON ohlcv_bars
    FOR EACH ROW
    EXECUTE FUNCTION _set_ohlcv_bars_updated_at();

-- Index on updated_at lets the scheduler efficiently check
-- "any bars updated since I last looked?"
CREATE INDEX IF NOT EXISTS idx_bars_updated_at
    ON ohlcv_bars (ticker_id, timeframe, updated_at);

INSERT INTO schema_migrations (version) VALUES ('006') ON CONFLICT DO NOTHING;
