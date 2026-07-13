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

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_RS_WINDOW = 4
_LEADLAG_WINDOWS = (52, 260)


def _weekly_log_returns(weekly_vals):
    mondays = sorted(weekly_vals)
    out = {}
    for i in range(1, len(mondays)):
        v0 = weekly_vals[mondays[i - 1]]
        v1 = weekly_vals[mondays[i]]
        if v0 and v1 and v0 > 0.0 and v1 > 0.0:
            out[mondays[i]] = math.log(v1 / v0)
    return out


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
    weight_periods: dict = {}
    for c in cons:
        fm = week_of(c.effective_from)
        to = week_of(c.effective_to) if c.effective_to else None
        weight_periods.setdefault(c.ticker_id, []).append(
            (fm, to, c.weight_at_rebalance)
        )
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

    ll_rows = _leadlag_rows(
        conn, industry, session, closes, weight_periods, week_of,
    )
    return rs_rows, ll_rows


def _leadlag_rows(conn, industry, session, closes, weight_periods, week_of):
    idx_rows = conn.execute(
        text("SELECT trade_date, value FROM industry_index_value "
             "WHERE industry_name = :ind ORDER BY trade_date"),
        {"ind": industry},
    ).all()
    if not idx_rows:
        return []
    idx_weekly: dict = {}
    for r in idx_rows:
        idx_weekly[week_of(r.trade_date)] = r.value
    r_I_map = _weekly_log_returns(idx_weekly)
    if len(r_I_map) < min(_LEADLAG_WINDOWS):
        return []
    idx_weeks = sorted(r_I_map)

    out: list[SectorLeadlagMetric] = []
    for t, cmap in closes.items():
        r_s_map = _weekly_log_returns(cmap)
        common = [w for w in idx_weeks if w in r_s_map]
        if len(common) < min(_LEADLAG_WINDOWS):
            continue
        r_s = [r_s_map[w] for w in common]
        r_i = [r_I_map[w] for w in common]
        w_s = []
        isc = []
        for w in common:
            wt = 0.0
            is_c = False
            for (fm, to, weight) in weight_periods.get(t, []):
                if w >= fm and (to is None or w <= to):
                    wt = weight
                    is_c = True
                    break
            w_s.append(wt)
            isc.append(is_c)
        for window in _LEADLAG_WINDOWS:
            res = compute_sector_leadlag(r_s, r_i, w_s, window=window)
            for i, m in enumerate(common):
                cell = res[i]
                if cell is None:
                    continue
                out.append(SectorLeadlagMetric(
                    ticker_id=t,
                    asof_date=m.astimezone(session.zone).date(),
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
