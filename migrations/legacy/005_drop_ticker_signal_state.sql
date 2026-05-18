-- 005_drop_ticker_signal_state.sql
-- Per the Phase 6 decision: ticker_signal_state is dropped.
-- The system rebuilds engine state from raw OHLCV bars on startup (Option A,
-- decided earlier), so this audit table provides no operational value and
-- only generates write amplification on every compute pass.

DROP TABLE IF EXISTS ticker_signal_state;

INSERT INTO schema_migrations (version) VALUES ('005') ON CONFLICT DO NOTHING;
