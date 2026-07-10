"""Industry x industry regime correlation matrix (spec §17). Pure (numpy).

Given a per-industry weekly regime proxy g^I (breadth), residualise each
against the cross-industry mean (kill the common-market factor, §17.2),
then compute the pairwise lead/lag correlation over the trailing window.
best_lag<0 for pair (I,J) means J's past predicts I -> J LEADS I.
"""
from __future__ import annotations

import numpy as np

_K = 4


def _corr(a, b):
    if len(a) < 3 or a.std() == 0.0 or b.std() == 0.0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def compute_regime_matrix(regime_by_industry, window=260, k=_K):
    """regime_by_industry: {industry: {week: g}}. Returns
    (rows, asof_week) where rows are tuples
    (industry_i, industry_j, corr, best_lag, lead_lag_corr, n_obs) over the
    trailing `window` weeks, residualised vs the cross-industry mean."""
    inds = sorted(regime_by_industry)
    all_weeks = sorted(set().union(
        *[set(s) for s in regime_by_industry.values()]
    )) if regime_by_industry else []
    weeks = all_weeks[-window:] if len(all_weeks) > window else all_weeks
    if len(weeks) < 3:
        return [], (weeks[-1] if weeks else None)
    widx = {w: i for i, w in enumerate(weeks)}

    g = np.full((len(inds), len(weeks)), np.nan)
    for ii, ind in enumerate(inds):
        for w, val in regime_by_industry[ind].items():
            if w in widx:
                g[ii, widx[w]] = val
    with np.errstate(invalid="ignore"):
        g_m = np.nanmean(g, axis=0)
    resid = g - g_m

    rows = []
    for a in range(len(inds)):
        xa = resid[a]
        for b in range(len(inds)):
            if a == b:
                continue
            xb = resid[b]
            mask = ~np.isnan(xa) & ~np.isnan(xb)
            if int(mask.sum()) < 3:
                continue
            base = _corr(xa[mask], xb[mask])
            if base is None:
                continue
            best_lag, best = 0, base
            for lag in range(-k, k + 1):
                if lag == 0:
                    rho = base
                elif lag > 0:
                    ia, ib = xa[:-lag], xb[lag:]
                    m = ~np.isnan(ia) & ~np.isnan(ib)
                    rho = _corr(ia[m], ib[m]) if int(m.sum()) >= 3 else None
                else:
                    ia, ib = xa[-lag:], xb[:lag]
                    m = ~np.isnan(ia) & ~np.isnan(ib)
                    rho = _corr(ia[m], ib[m]) if int(m.sum()) >= 3 else None
                if rho is not None and abs(rho) > abs(best):
                    best_lag, best = lag, rho
            rows.append((
                inds[a], inds[b], base, best_lag, best, int(mask.sum()),
            ))
    return rows, weeks[-1]
