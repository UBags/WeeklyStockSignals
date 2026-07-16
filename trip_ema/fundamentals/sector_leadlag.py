"""Sector-index <-> stock correlation & lead/lag (spec §16). Pure (numpy).

Given a stock's weekly log returns, its industry index's weekly log
returns, and the stock's index weight per week, compute per-week rolling
correlation / lead-lag / beta vs the leave-one-out (self-inclusion-
debiased) index return, and a LEADER/LAGGARD/COUPLED/DECOUPLED class.
"""
from __future__ import annotations

import numpy as np

_K = 10


def _rankdata(a):
    # average ranks for ties; a is 1-D float
    _, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    starts = csum - counts
    avg = (starts + csum - 1) / 2.0
    return avg[inv]


def _spearman(a, b):
    # Spearman rank-correlation via numpy rank-then-Pearson (no scipy dep).
    if len(a) < 3 or a.std() == 0.0 or b.std() == 0.0:
        return None
    ra, rb = _rankdata(a), _rankdata(b)
    return float(np.corrcoef(ra, rb)[0, 1])


def compute_sector_leadlag(r_s, r_I, w_s, window, k=_K):
    """r_s, r_I, w_s: 1-D float arrays aligned by week (ascending).
    window: trailing correlation window (weeks). Returns a list aligned
    with the input; each entry is None (insufficient history) or a dict
    {insync_corr, best_lag, lead_lag_corr, beta_sector, n_obs,
     coupling_class}."""
    r_s = np.asarray(r_s, float)
    r_I = np.asarray(r_I, float)
    w_s = np.asarray(w_s, float)
    n = len(r_s)
    denom = 1.0 - w_s
    safe = np.where(denom != 0.0, denom, 1.0)
    r_Is = np.where(w_s > 0.0, (r_I - w_s * r_s) / safe, r_I)

    out = [None] * n
    for t in range(n):
        lo = t - window + 1
        if lo < k:
            continue
        s_win = r_s[lo:t + 1]
        I_win = r_Is[lo:t + 1]
        insync = _spearman(s_win, I_win)
        if insync is None:
            continue
        best_lag, best_rho = 0, insync
        for lag in range(-k, k + 1):
            if lag == 0:
                rho = insync
            elif lag > 0:
                rho = _spearman(r_s[lo:t + 1 - lag], r_Is[lo + lag:t + 1])
            else:
                rho = _spearman(r_s[lo - lag:t + 1], r_Is[lo:t + 1 + lag])
            if rho is not None and abs(rho) > abs(best_rho):
                best_lag, best_rho = lag, rho
        var_I = float(I_win.var())
        beta = (float(np.cov(s_win, I_win)[0, 1]) / var_I
                if var_I > 0.0 else None)
        n_obs = len(s_win)
        floor = 2.0 / (n_obs ** 0.5)
        if abs(best_rho) <= floor:
            cls = "DECOUPLED"
        elif best_lag > 0:
            cls = "LEADER"
        elif best_lag < 0:
            cls = "LAGGARD"
        else:
            cls = "COUPLED"
        out[t] = {
            "insync_corr": insync, "best_lag": best_lag,
            "lead_lag_corr": best_rho, "beta_sector": beta,
            "n_obs": n_obs, "coupling_class": cls,
        }
    return out
