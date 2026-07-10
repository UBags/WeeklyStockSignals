"""Influential-industry ensemble selection (spec §17.4). Pure.

From the industry x industry regime matrix, pick each industry's ensemble
under three (theta, M) variants so downstream models can compare coupling
strictness. Membership is by contemporaneous |corr| >= theta, top-M;
is_leader flags predictive members (best_lag < 0); sign splits co-movers
(+1) from counter-movers (-1).
"""
from __future__ import annotations


def _theta_floor(n):
    return 2.0 / (n ** 0.5)


def _theta_mid(n):
    return max(2.0 / (n ** 0.5), 0.15)


def _theta_strict(n):
    return 0.25


VARIANTS = {
    "floor": (_theta_floor, 10),
    "mid": (_theta_mid, 10),
    "strict": (_theta_strict, 8),
}


def compute_ensembles(matrix_rows, variants=VARIANTS):
    """matrix_rows: iterable of (i, j, corr, best_lag, lead_lag_corr, n_obs).
    Returns list of (industry_i, member_j, variant, corr, best_lag,
    is_leader, sign)."""
    by_i: dict = {}
    for (i, j, corr, best_lag, _llc, n_obs) in matrix_rows:
        by_i.setdefault(i, []).append((j, corr, best_lag, n_obs))

    out = []
    for i, members in by_i.items():
        for vname, (theta_fn, cap) in variants.items():
            elig = [
                (j, corr, bl, n) for (j, corr, bl, n) in members
                if abs(corr) >= theta_fn(n)
            ]
            elig.sort(key=lambda x: abs(x[1]), reverse=True)
            for (j, corr, bl, _n) in elig[:cap]:
                out.append((
                    i, j, vname, corr, bl, bl < 0,
                    1 if corr > 0 else -1,
                ))
    return out
