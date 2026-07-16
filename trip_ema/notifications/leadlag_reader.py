"""DB reader: build a LeadLagContext for one signal (design §6.1).

Reads the precomputed lead/lag tables and derives the industry regime state on
the fly (design §5 — no materialised events table). The pure derivation and the
dataclasses live in leadlag_render; this module is only the queries + assembly,
so it stays thin and the derivation stays unit-tested there.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .leadlag_render import (
    Corroborator,
    LeadLagContext,
    changed_weeks_ago,
    regime_sign,
)

# Balanced ensemble variant for the operator email (§8 #4 — floor is noisy,
# strict too aggressive).
_ENSEMBLE_VARIANT = "mid"

_STOCK_SQL = text(
    """
    SELECT best_lag, lead_lag_corr, coupling_class
    FROM sector_stock_leadlag
    WHERE ticker_id = :tid
    ORDER BY asof_date DESC, "window" DESC
    LIMIT 1
    """
)

_ENSEMBLE_SQL = text(
    """
    SELECT member_j, corr, best_lag, sign, is_leader
    FROM influential_industry_ensemble
    WHERE industry_i = :ind AND variant = :v
    ORDER BY abs(corr) DESC
    """
)

_BREADTH_SQL = text(
    """
    SELECT industry_name, breadth
    FROM industry_regime_breadth
    WHERE industry_name = ANY(:inds)
    ORDER BY industry_name, asof_date
    """
)


def _regime_state(breadth_series: list[float]) -> tuple[int, int | None]:
    """(regime_now, changed_weeks_ago) from an ascending breadth series."""
    if not breadth_series:
        return 0, None
    signs = [regime_sign(b) for b in breadth_series]
    return signs[-1], changed_weeks_ago(signs)


def load_leadlag_context(
    conn: Connection, ticker_id: int, industry: str,
    *,
    max_corroborators: int = 3,
) -> LeadLagContext:
    """Assemble the lead/lag context for a signal on `ticker_id` in
    `industry`. Every field degrades to None/empty when its table is thin, so
    the renderer can emit nothing rather than a broken sentence."""
    srow = conn.execute(_STOCK_SQL, {"tid": ticker_id}).one_or_none()

    ens = conn.execute(
        _ENSEMBLE_SQL, {"ind": industry, "v": _ENSEMBLE_VARIANT},
    ).all()

    inds = [industry] + [r.member_j for r in ens]
    series: dict = {}
    for r in conn.execute(_BREADTH_SQL, {"inds": inds}).all():
        series.setdefault(r.industry_name, []).append(r.breadth)

    ind_regime_now, ind_changed = _regime_state(series.get(industry, []))

    corroborators = []
    for r in ens:
        rn, cwa = _regime_state(series.get(r.member_j, []))
        corroborators.append(Corroborator(
            industry=r.member_j,
            corr=r.corr,
            best_lag=r.best_lag,
            sign=r.sign,
            is_leader=r.is_leader,
            regime_now=rn,
            changed_weeks_ago=cwa,
        ))

    return LeadLagContext(
        stock_best_lag=(srow.best_lag if srow else None),
        stock_corr=(srow.lead_lag_corr if srow else None),
        stock_coupling=(srow.coupling_class if srow else None),
        industry_regime_now=ind_regime_now,
        industry_changed_weeks_ago=ind_changed,
        corroborators=tuple(corroborators[:max_corroborators]),
    )
