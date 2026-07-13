-- 0002: drop tickers.tz
--
-- SELECT tz, COUNT(*) FROM tickers GROUP BY tz;
--  tz  | count
-- -----+-------
--  UTC |  2685
--
-- The column said every NSE ticker was in UTC. It was never authoritative: it was
-- passed as `fallback_tz` to market_session_for(exchange, tz), and for any KNOWN
-- exchange the spec wins and the value is ignored. Its only effect was on the
-- UNKNOWN-exchange path, where it produced a generic 09:00-16:00 session in
-- 'UTC' — silently plausible, and wrong.
--
-- market_session_for now RAISES on an unknown exchange, so the fallback is gone
-- and with it the last use of this column. config.yaml's market_hours is the
-- single source of truth for what a market is.
--
-- A column that lies is worse than no column.

ALTER TABLE tickers DROP COLUMN IF EXISTS tz;
