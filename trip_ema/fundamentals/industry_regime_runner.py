"""Weekly recompute of the industry x industry regime correlation matrix
(spec §17). Regime proxy = breadth (mean constituent display_regime) per
industry per week; residualised vs the cross-industry mean; pairwise
lead/lag correlation over the trailing 5y. Single current snapshot.
"""
from __future__ import annotations

import logging

from sqlalchemy import Engine, text

from ..domain.industry_regime_corr import IndustryRegimeCorr
from ..persistence import transaction
from ..persistence.industry_regime_repo import IndustryRegimeCorrRepository
from .market_session import market_session_for
from .regime_matrix import compute_regime_matrix

logger = logging.getLogger(__name__)

_WINDOW = 260

_BREADTH_SQL = text(
    """
    WITH weekly AS (
        SELECT DISTINCT ON (sis.ticker_id, sis.bar_timestamp)
               sis.ticker_id, sis.bar_timestamp, sis.date,
               sis.display_regime
        FROM stock_indicator_state sis
        WHERE sis.timeframe = '1W'
        ORDER BY sis.ticker_id, sis.bar_timestamp, sis.date DESC
    )
    SELECT iic.industry_name AS industry,
           w.bar_timestamp   AS wk,
           avg(w.display_regime::float) AS breadth
    FROM industry_index_constituent iic
    JOIN weekly w ON w.ticker_id = iic.ticker_id
    WHERE w.date >= iic.effective_from
      AND (iic.effective_to IS NULL OR w.date <= iic.effective_to)
    GROUP BY 1, 2
    """
)


def _breadth_by_industry(conn):
    out: dict = {}
    for r in conn.execute(_BREADTH_SQL).all():
        out.setdefault(r.industry, {})[r.wk] = r.breadth
    return out


def backfill_industry_regime_matrix(
    db_engine: Engine,
    exchange: str,
    *,
    window: int = _WINDOW,
    progress_cb=None,
):
    """Recompute the whole industry x industry regime matrix (one snapshot).
    Not fanned out — the matrix is a single global computation."""
    session = market_session_for(exchange)
    with transaction(db_engine) as conn:
        regime = _breadth_by_industry(conn)
        if len(regime) < 2:
            logger.info("regime matrix: <2 industries, skipping")
            return 0
        rows, asof_wk = compute_regime_matrix(regime, window=window)
        if not rows or asof_wk is None:
            logger.info("regime matrix: no rows produced")
            return 0
        asof = asof_wk.astimezone(session.zone).date()
        metrics = [
            IndustryRegimeCorr(
                industry_i=i, industry_j=j, window=window, asof_date=asof,
                corr=c, best_lag=bl, lead_lag_corr=llc, n_obs=n,
            )
            for (i, j, c, bl, llc, n) in rows
        ]
        n_up = IndustryRegimeCorrRepository(conn).upsert_many(metrics)
    logger.info(
        "regime matrix: %d industries -> %d pairs (asof %s)",
        len(regime), n_up, asof,
    )
    return n_up
