"""Weekly per-industry recompute of sector_stock_leadlag (spec §16.4).

Fans out over indexed industries (not tickers): for each industry it loads
the constituent set (with active [effective_from, effective_to] windows),
loads each constituent's weekly closes once, computes the cross-sectional
rs_rank per week, and upserts. Because constituents are loaded per industry
this avoids the N^2 blow-up of a per-ticker pass. Re-runnable; rs_rank for
a week finalises as constituents' bars fill in.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, time

from sqlalchemy import Engine, text

from ..domain import Timeframe
from ..domain.sector_metric import SectorStockMetric
from ..indicators.aggregation import _monday_of, daily_to_weekly
from ..persistence import transaction
from ..persistence.bars_repo import BarsRepository
from ..persistence.sector_metrics_repo import SectorStockLeadlagRepository
from .market_session import market_session_for
from .parallel import fan_out
from .sector_cross_section import compute_rs_rank_by_week

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _metrics_for_industry(conn, industry, session, window):
    cons = conn.execute(
        text(
            "SELECT ticker_id, effective_from, effective_to "
            "FROM industry_index_constituent WHERE industry_name = :ind"
        ),
        {"ind": industry},
    ).all()
    if not cons:
        return []

    def week_of(d):
        return _monday_of(
            datetime.combine(d, time(0, 0), tzinfo=session.zone),
            tz=session.tz,
        )

    # union each ticker's constituent periods into one active window
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

    # ranked set = every stock mapped to this industry, plus the
    # constituents; reference (distribution) set = the constituents.
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
        closes, reference_closes, active, window=window,
    )
    out: list[SectorStockMetric] = []
    for t, rmap in ranks.items():
        for monday, rr in rmap.items():
            if rr is None or (isinstance(rr, float) and math.isnan(rr)):
                continue
            out.append(SectorStockMetric(
                ticker_id=t,
                asof_date=monday.astimezone(session.zone).date(),
                window=window, industry_name=industry, rs_rank=rr,
            ))
    return out


def backfill_sector_metrics(
    db_engine: Engine,
    exchange: str,
    *,
    window: int = 4,
    progress_cb=None,
):
    """Recompute sector_stock_leadlag.rs_rank for every ACTIVE indexed
    industry. Idempotent per (ticker, week, window)."""
    session = market_session_for(exchange)
    with transaction(db_engine) as conn:
        industries = [
            r.industry_name
            for r in conn.execute(
                text(
                    "SELECT industry_name FROM industry_index "
                    "WHERE status = 'ACTIVE'"
                )
            ).all()
        ]

    def _one(industry):
        with transaction(db_engine) as conn:
            rows = _metrics_for_industry(conn, industry, session, window)
            if rows:
                SectorStockLeadlagRepository(conn).upsert_many(rows)
            return len(rows)

    outcome = fan_out(
        industries, _one, label="sector_metrics", progress_cb=progress_cb,
    )
    logger.info(
        "backfill_sector_metrics %s: %d industries, %d failed",
        exchange, outcome.done, len(outcome.failed),
    )
    return outcome
