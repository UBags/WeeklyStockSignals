-- ============================================================
-- RELIANCE diagnostic: where is it in the database?
-- ============================================================

\echo '----- 1. Does RELIANCE exist in the tickers table? -----'
SELECT ticker_id, exchange, symbol, instrument_type AS instrument,
       full_name, is_active
FROM tickers
WHERE symbol = 'RELIANCE'
ORDER BY exchange, instrument_type;

\echo ''
\echo '----- 2. How many OHLCV bars does RELIANCE have, by timeframe? -----'
SELECT t.ticker_id, t.exchange, t.symbol,
       b.timeframe,
       COUNT(*)               AS bar_count,
       MIN(b.bar_timestamp)::date AS earliest,
       MAX(b.bar_timestamp)::date AS latest
FROM tickers t
LEFT JOIN ohlcv_bars b ON b.ticker_id = t.ticker_id
WHERE t.symbol = 'RELIANCE'
GROUP BY t.ticker_id, t.exchange, t.symbol, b.timeframe
ORDER BY t.exchange, b.timeframe NULLS LAST;

\echo ''
\echo '----- 3. What distinct timeframes exist across the WHOLE ohlcv_bars table? -----'
\echo '       (tells us which timeframes are actually being ingested at all)'
SELECT timeframe, COUNT(*) AS bar_count,
       COUNT(DISTINCT ticker_id) AS distinct_tickers,
       MIN(bar_timestamp)::date AS earliest,
       MAX(bar_timestamp)::date AS latest
FROM ohlcv_bars
GROUP BY timeframe
ORDER BY timeframe;

\echo ''
\echo '----- 4. What about other things we have for RELIANCE? -----'
\echo '       (sanity check that fundamentals data DID populate)'
SELECT 'instrument_industry' AS table_name,
       COUNT(*) AS row_count
FROM instrument_industry ii
JOIN tickers t ON t.ticker_id = ii.ticker_id
WHERE t.symbol = 'RELIANCE'
UNION ALL
SELECT 'instrument_shares',
       COUNT(*)
FROM instrument_shares s
JOIN tickers t ON t.ticker_id = s.ticker_id
WHERE t.symbol = 'RELIANCE'
UNION ALL
SELECT 'quarterly_financials',
       COUNT(*)
FROM quarterly_financials qf
JOIN tickers t ON t.ticker_id = qf.ticker_id
WHERE t.symbol = 'RELIANCE'
UNION ALL
SELECT 'daily_market_cap',
       COUNT(*)
FROM daily_market_cap mc
JOIN tickers t ON t.ticker_id = mc.ticker_id
WHERE t.symbol = 'RELIANCE';