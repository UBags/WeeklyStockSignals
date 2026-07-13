"""Compute per-signal ensemble features (spec §17.5).

Loads the influential ensembles and the industry breadth series into
memory (both small), then walks every signal: stock -> industry I -> I's
ensemble; aggregate members' regime at the signal's week into the four
features, per ensemble variant. Read-time join by signal_enrichment_id.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import Engine, text

from ..domain.signal_ensemble_features import SignalEnsembleFeatures
from ..persistence import transaction
from ..persistence.signal_ensemble_repo import (
    SignalEnsembleFeaturesRepository,
)
from .ensemble_features import compute_ensemble_features
from ..domain.market_session import market_session_for

logger = logging.getLogger(__name__)

_BULLISH = {"BUY", "EXIT_FROM_SELL"}


def _week_monday(d):
    return d - timedelta(days=d.weekday())


def backfill_signal_ensemble_features(
    db_engine: Engine,
    exchange: str,
    *,
    progress_cb=None,
):
    market_session_for(exchange)  # validate exchange
    with transaction(db_engine) as conn:
        tick_ind = {
            r.ticker_id: r.industry
            for r in conn.execute(
                text("SELECT ticker_id, industry FROM instrument_industry")
            ).all()
        }
        ens: dict = {}
        for r in conn.execute(text(
            "SELECT industry_i, member_j, variant, corr, is_leader "
            "FROM influential_industry_ensemble"
        )).all():
            ens.setdefault(r.industry_i, {}).setdefault(
                r.variant, []
            ).append((r.member_j, r.corr, r.is_leader))
        breadth = {
            (r.industry_name, r.asof_date): r.breadth
            for r in conn.execute(text(
                "SELECT industry_name, asof_date, breadth "
                "FROM industry_regime_breadth"
            )).all()
        }
        variants = sorted({
            v for by_v in ens.values() for v in by_v
        })

        out: list[SignalEnsembleFeatures] = []
        for s in conn.execute(text(
            "SELECT signal_enrichment_id, ticker_id, signal_type, "
            "signal_date FROM signal_enrichment"
        )).all():
            ind = tick_ind.get(s.ticker_id)
            if ind is None or ind not in ens:
                continue
            wk = _week_monday(s.signal_date)
            sdir = 1 if s.signal_type in _BULLISH else -1
            for v in variants:
                members = ens[ind].get(v, [])
                mm = [
                    (corr, breadth.get((mj, wk)), isl)
                    for (mj, corr, isl) in members
                ]
                res = compute_ensemble_features(sdir, mm)
                if res is None:
                    continue
                score, confirms, agreement, n_bull, n_bear = res
                out.append(SignalEnsembleFeatures(
                    signal_enrichment_id=s.signal_enrichment_id,
                    variant=v, ensemble_score=score,
                    ensemble_confirms=confirms,
                    ensemble_agreement=agreement,
                    n_leaders_bull=n_bull, n_leaders_bear=n_bear,
                ))
        n = SignalEnsembleFeaturesRepository(conn).upsert_many(out)
    logger.info(
        "signal ensemble features %s: %d rows over %d variants",
        exchange, n, len(variants),
    )
    return n
