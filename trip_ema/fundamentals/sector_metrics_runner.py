"""Weekly per-industry recompute of sector_stock_leadlag (§11b + §16).

Fans out over indexed industries (not tickers). Per industry it loads the
constituent set (weights + active windows), the industry index value
series, and every industry stock's weekly closes once, then computes:
  - rs_rank (§11b): cross-sectional pctrank of each stock's N-week return
    among the constituents (window=4).
  - sector lead/lag (§16): rolling correlation / lead-lag / beta of each
    stock vs the leave-one-out industry index, at windows 52 and 260.
Both are upserted into sector_stock_leadlag. Loading bars per industry
(shared across both computes) avoids the N^2 blow-up of a per-ticker pass.
Re-runnable.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, time

from sqlalchemy import Engine, text

from ..domain import Timeframe
from ..domain.sector_leadlag_metric import SectorLeadlagMetric
from ..domain.sector_metric import SectorStockMetric
from ..indicators.aggregation import _monday_of, daily_to_weekly
from ..persistence import transaction
from ..persistence.bars_repo import BarsRepository
from ..persistence.sector_metrics_repo import SectorStockLeadlagRepository
from ..domain.market_session import market_session_for
from .parallel import fan_out
from .sector_cross_section import compute_rs_rank_by_week
from .sector_leadlag import compute_sector_leadlag
from .sector_leadlag_inputs import build_regime_leadlag_inputs

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_RS_WINDOW = 4
_LEADLAG_WINDOWS = (52, 260)



def _metrics_for_industry(conn, industry, session):
    """Return (rs_rank_rows, leadlag_rows) for one industry."""
    cons = conn.execute(
        text(
            "SELECT ticker_id, effective_from, effective_to, "
            "weight_at_rebalance "
            "FROM industry_index_constituent WHERE industry_name = :ind"
        ),
        {"ind": industry},
    ).all()
    if not cons:
        return [], []

    def week_of(d):
        return _monday_of(
            datetime.combine(d, time(0, 0), tzinfo=session.zone),
            tz=session.tz,
        )

    active: dict = {}
    for c in cons:
        fm = week_of(c.effective_from)
        to = week_of(c.effective_to) if c.effective_to else None
        if c.ticker_id in active:
            pf, pt = active[c.ticker_id]
            active[c.ticker_id] = (
                min(pf, fm),
                None if (pt is None or to is None) else max(pt, to),
            )
        else:
            active[c.ticker_id] = (fm, to)

    ranked_ids = {
        r.ticker_id
        for r in conn.execute(
            text("SELECT ticker_id FROM instrument_industry "
                 "WHERE industry = :ind"),
            {"ind": industry},
        ).all()
    }
    ranked_ids |= set(active)

    closes: dict = {}
    for t in ranked_ids:
        daily = BarsRepository(conn).fetch_since(t, Timeframe.DAILY, _EPOCH)
        if not daily:
            continue
        weekly = daily_to_weekly(
            daily, include_partial_last_week=False, tz=session.tz,
        )
        closes[t] = {b.timestamp: b.close for b in weekly}

    reference_closes = {t: closes[t] for t in active if t in closes}
    ranks = compute_rs_rank_by_week(
        closes, reference_closes, active, window=_RS_WINDOW,
    )
    rs_rows: list[SectorStockMetric] = []
    for t, rmap in ranks.items():
        for monday, rr in rmap.items():
            if rr is None or (isinstance(rr, float) and math.isnan(rr)):
                continue
            rs_rows.append(SectorStockMetric(
                ticker_id=t,
                asof_date=monday.astimezone(session.zone).date(),
                window=_RS_WINDOW, industry_name=industry, rs_rank=rr,
            ))

    ll_rows = _leadlag_rows(conn, industry, session, ranked_ids)
    return rs_rows, ll_rows


def _leadlag_rows(conn, industry, session, ranked_ids):
    """Regime-based sector lead/lag (design §4.2). Correlates each ranked
    stock's weekly display_regime against the industry BREADTH (equal-weight
    mean constituent regime), leave-one-out-debiased at w_s = 1/N on the weeks
    the stock is a breadth constituent (see sector_leadlag_inputs).

    Weeks are keyed by stock_indicator_state.bar_timestamp so a stock's regime
    and the breadth align on the identical instant. The per-(ticker, week)
    value is the LATEST daily 1W regime in that week — the same DISTINCT ON the
    industry-matrix breadth uses — so the 1/N debias subtraction is exact for a
    constituent. All active constituents are a subset of ranked_ids, so the
    breadth built from the ids-filtered CTE is complete."""
    if not ranked_ids:
        return []
    ids = list(ranked_ids)

    weekly_cte = (
        "WITH weekly AS ("
        " SELECT DISTINCT ON (sis.ticker_id, sis.bar_timestamp)"
        " sis.ticker_id, sis.bar_timestamp AS wk, sis.date,"
        " sis.display_regime"
        " FROM stock_indicator_state sis"
        " WHERE sis.timeframe = '1W' AND sis.ticker_id = ANY(:ids)"
        " ORDER BY sis.ticker_id, sis.bar_timestamp, sis.date DESC )"
    )

    breadth: dict = {}
    n_by_week: dict = {}
    for r in conn.execute(
        text(
            weekly_cte
            + " SELECT w.wk AS wk,"
              " avg(w.display_regime::float) AS breadth, count(*) AS n"
              " FROM industry_index_constituent iic"
              " JOIN weekly w ON w.ticker_id = iic.ticker_id"
              " WHERE iic.industry_name = :ind"
              " AND w.date >= iic.effective_from"
              " AND (iic.effective_to IS NULL OR w.date <= iic.effective_to)"
              " GROUP BY w.wk"
        ),
        {"ind": industry, "ids": ids},
    ).all():
        breadth[r.wk] = r.breadth
        n_by_week[r.wk] = r.n
    if len(breadth) < min(_LEADLAG_WINDOWS):
        return []

    stock_regime: dict = {}
    for r in conn.execute(
        text(
            weekly_cte
            + " SELECT w.ticker_id, w.wk AS wk, w.display_regime FROM weekly w"
        ),
        {"ids": ids},
    ).all():
        stock_regime.setdefault(r.ticker_id, {})[r.wk] = r.display_regime

    cons_weeks: dict = {}
    for r in conn.execute(
        text(
            weekly_cte
            + " SELECT w.ticker_id, w.wk AS wk"
              " FROM industry_index_constituent iic"
              " JOIN weekly w ON w.ticker_id = iic.ticker_id"
              " WHERE iic.industry_name = :ind"
              " AND w.date >= iic.effective_from"
              " AND (iic.effective_to IS NULL OR w.date <= iic.effective_to)"
        ),
        {"ind": industry, "ids": ids},
    ).all():
        cons_weeks.setdefault(r.ticker_id, set()).add(r.wk)

    out: list[SectorLeadlagMetric] = []
    for tid, regime_map in stock_regime.items():
        weeks, r_s, r_I, w_s, isc = build_regime_leadlag_inputs(
            regime_map, breadth, n_by_week, cons_weeks.get(tid, set()),
        )
        if len(weeks) < min(_LEADLAG_WINDOWS):
            continue
        for window in _LEADLAG_WINDOWS:
            res = compute_sector_leadlag(r_s, r_I, w_s, window=window)
            for i, wk in enumerate(weeks):
                cell = res[i]
                if cell is None:
                    continue
                out.append(SectorLeadlagMetric(
                    ticker_id=tid,
                    asof_date=wk.astimezone(session.zone).date(),
                    window=window, industry_name=industry,
                    is_constituent=isc[i],
                    insync_corr=cell["insync_corr"],
                    best_lag=cell["best_lag"],
                    lead_lag_corr=cell["lead_lag_corr"],
                    beta_sector=cell["beta_sector"],
                    n_obs=cell["n_obs"],
                    coupling_class=cell["coupling_class"],
                ))
    return out


def backfill_sector_metrics(
    db_engine: Engine,
    exchange: str,
    *,
    progress_cb=None,
):
    """Recompute sector_stock_leadlag (rs_rank + sector lead/lag) for every
    ACTIVE indexed industry. Idempotent per (ticker, week, window)."""
    session = market_session_for(exchange)
    with transaction(db_engine) as conn:
        industries = [
            r.industry_name
            for r in conn.execute(
                text("SELECT industry_name FROM industry_index "
                     "WHERE status = 'ACTIVE'")
            ).all()
        ]

    def _one(industry):
        with transaction(db_engine) as conn:
            rs_rows, ll_rows = _metrics_for_industry(conn, industry, session)
            repo = SectorStockLeadlagRepository(conn)
            n = 0
            if rs_rows:
                n += repo.upsert_many(rs_rows)
            if ll_rows:
                n += repo.upsert_leadlag_many(ll_rows)
            return n

    outcome = fan_out(
        industries, _one, label="sector_metrics", progress_cb=progress_cb,
    )
    logger.info(
        "backfill_sector_metrics %s: %d industries, %d failed",
        exchange, outcome.done, len(outcome.failed),
    )
    return outcome
