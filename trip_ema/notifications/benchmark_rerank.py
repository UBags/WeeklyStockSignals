"""Phase 6-3: Composite rank-score query for email BUY ordering.

Builds the per-ticker rank score from stock_benchmark_stats +
industry_benchmark_stats:

    rank_score = 0.5 × stock_RS_26w (vs INDUSTRY benchmark)
               + 0.3 × industry_RS_26w (industry vs NIFTY)
               + 0.2 × R²_26w (stock vs INDUSTRY)

All three components use the SHORT window (26 weekly returns).
COALESCE(...) = 0 means tickers without stats (sub-threshold history,
or industry sub-threshold) get score=0 — they sort to the middle of
the BUY section rather than the bottom, which would be wrong: lacking
stats is "unknown momentum", not "bad momentum".

Why these three components and these weights:
  - stock_RS dominates at 0.5: RS = (stock return − benchmark return)
    over the window. The single most predictive momentum metric in
    the empirical literature; if we could only pick one, it'd be this.
  - industry_RS at 0.3: surfacing tickers in strong industries on top
    of those in weak industries. A BUY in a hot sector beats a BUY in
    a cold sector, all else equal.
  - R² at 0.2: a credibility filter. Tickers with high RS but β/R²
    that don't track the benchmark are more likely to be noise. The
    R² weight pulls them down without hard-filtering them out.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


def fetch_rank_scores(
    conn: Connection,
    ticker_ids: Sequence[int],
) -> dict[int, float]:
    """Return {ticker_id: rank_score} for a given set of tickers.

    Tickers not in stock_benchmark_stats yet (sub-threshold history,
    or pre-Phase-6-3 deploys) are absent from the returned dict —
    callers should treat missing as score=0 (neutral). The SQL
    treats them as 0 internally so the JOIN doesn't drop the ticker;
    we materialize all input ticker_ids in the response.

    One SQL roundtrip:
      stock_short:    DISTINCT ON (ticker_id) — latest as_of_date row
                      per ticker, INDUSTRY benchmark, SHORT window.
      industry_short: DISTINCT ON (industry_name) — latest as_of_date
                      row per industry, SHORT window.

    Both subqueries are O(N log N) with the existing
    idx_*_benchmark_stats_latest indexes — sub-100ms even at
    full-universe scale.
    """
    if not ticker_ids:
        return {}

    rows = conn.execute(text("""
        WITH stock_short AS (
            SELECT DISTINCT ON (ticker_id)
                ticker_id,
                rs_pct    AS stock_rs_26w,
                r_squared AS stock_r2_26w
            FROM stock_benchmark_stats
            WHERE benchmark_kind = 'INDUSTRY'
              AND window_label   = 'SHORT'
              AND ticker_id      = ANY(:tids)
            ORDER BY ticker_id, as_of_date DESC
        ),
        industry_short AS (
            SELECT DISTINCT ON (industry_name)
                industry_name,
                rs_pct AS industry_rs_26w
            FROM industry_benchmark_stats
            WHERE window_label = 'SHORT'
            ORDER BY industry_name, as_of_date DESC
        )
        SELECT t.ticker_id,
               (
                   0.5 * COALESCE(ss.stock_rs_26w, 0)
                 + 0.3 * COALESCE(ins.industry_rs_26w, 0)
                 + 0.2 * COALESCE(ss.stock_r2_26w, 0)
               ) AS rank_score
        FROM tickers t
        LEFT JOIN stock_short    ss  ON ss.ticker_id    = t.ticker_id
        LEFT JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
        LEFT JOIN industry_short ins ON ins.industry_name = ii.industry
        WHERE t.ticker_id = ANY(:tids)
    """), {"tids": list(ticker_ids)}).all()

    scores: dict[int, float] = {
        int(r.ticker_id): float(r.rank_score) if r.rank_score is not None
                          else 0.0
        for r in rows
    }
    logger.debug(
        "fetch_rank_scores: requested=%d returned=%d "
        "(missing-from-stats get default 0.0 at sort time)",
        len(ticker_ids), len(scores),
    )
    return scores
