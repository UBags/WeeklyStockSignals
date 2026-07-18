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


def compute_sector_leadlag_reference(r_s, r_I, w_s, window, k=_K):
    """Original scalar implementation. RETAINED DELIBERATELY AS THE ORACLE.

    Superseded by the vectorised compute_sector_leadlag below, which must
    agree with this function exactly. Kept so
    tests/fundamentals/test_sector_leadlag_vectorised.py can diff the two on
    every run: this output reaches sector_stock_leadlag -> email enrichment ->
    ensemble features, so a numerical drift here is a wrong trading signal,
    not a cosmetic regression.

    Do not "clean this up" and do not delete it. It is the definition of
    correct, including the quirks the fast path reproduces on purpose:
      - np.cov's ddof=1 over ndarray.var()'s ddof=0 in `beta`
      - the strict-improvement lag scan, which leaves lag 0 winning ties
      - None (not NaN) for windows that are too short or degenerate

    r_s, r_I, w_s: 1-D float arrays aligned by week (ascending).
    window: trailing correlation window (weeks). Returns a list aligned
    with the input; each entry is None (insufficient history) or a dict
    {insync_corr, best_lag, lead_lag_corr, beta_sector, n_obs,
     coupling_class}.
    """
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


def _avg_ranks_2d(a2d):
    """Average ranks along axis=1, one row per window.

    Reproduces _rankdata exactly. _rankdata gives each tie group
    (group_start + group_end) / 2 in 0-based sorted positions; a stable
    argsort puts equal values adjacent, so the same two indices are
    recoverable with a forward max-scan (start) and a reverse min-scan (end).

    NOTE ON NaN: np.unique collapses all NaNs into one group, a stable
    argsort does not. The callers feed regime and breadth values that are
    never NaN (build_regime_leadlag_inputs coerces DB floats), so the two
    agree. If a NaN-producing source is ever added upstream, this assumption
    breaks silently -- filter before calling, do not patch around it here.
    """
    n, L = a2d.shape
    order = np.argsort(a2d, axis=1, kind="stable")
    s = np.take_along_axis(a2d, order, axis=1)
    idx = np.arange(L)

    new = np.empty((n, L), dtype=bool)
    new[:, 0] = True
    np.not_equal(s[:, 1:], s[:, :-1], out=new[:, 1:])
    start = np.maximum.accumulate(np.where(new, idx, 0), axis=1)

    endm = np.empty((n, L), dtype=bool)
    endm[:, L - 1] = True
    np.not_equal(s[:, 1:], s[:, :-1], out=endm[:, :-1])
    end = np.minimum.accumulate(
        np.where(endm, idx, L - 1)[:, ::-1], axis=1,
    )[:, ::-1]

    out = np.empty((n, L), dtype=float)
    np.put_along_axis(out, order, (start + end) / 2.0, axis=1)
    return out


def _win(a, length):
    """Sliding windows of `a` as an (n-length+1, length) VIEW. No copy."""
    return np.lib.stride_tricks.sliding_window_view(a, length)


def _rolling_spearman(A, B):
    """Row-wise Spearman for aligned window matrices.

    NaN marks the reference's None: fewer than 3 observations, or either
    window constant. The ddof factors in Pearson cancel, so this equals
    np.corrcoef on the ranks to floating-point rounding.
    """
    n, L = A.shape
    if L < 3:
        return np.full(n, np.nan)
    # Degeneracy is tested on the RAW windows, as the reference does -- not
    # on the ranks, which are never constant unless the raw window is.
    ok = (A.std(axis=1) != 0.0) & (B.std(axis=1) != 0.0)
    ra, rb = _avg_ranks_2d(A), _avg_ranks_2d(B)
    da = ra - ra.mean(axis=1, keepdims=True)
    db = rb - rb.mean(axis=1, keepdims=True)
    num = np.einsum("ij,ij->i", da, db)
    den = np.sqrt(
        np.einsum("ij,ij->i", da, da) * np.einsum("ij,ij->i", db, db),
    )
    out = np.full(n, np.nan)
    good = ok & (den > 0)
    out[good] = num[good] / den[good]
    return out


def compute_sector_leadlag(r_s, r_I, w_s, window, k=_K):
    """Vectorised twin of compute_sector_leadlag_reference. Same output.

    The scalar version calls _spearman n x (2k+1) times per series -- ~22,000
    calls of ~527us each on real data, which is 95% of stage_6b_sector_metrics.
    This does one batched pass per (window, lag) instead: 2k+1 sliding-window
    views, ranked and correlated along axis=1.

    Total sort work is unchanged; what disappears is per-call Python and numpy
    dispatch overhead. Measured 15x at window=52, 3x at window=260 (the larger
    window spends proportionally more time in the sort itself). Removing the
    sort entirely, via a rolling histogram over the small-cardinality regime
    values, is the next step and is NOT done here.

    Equivalence with the reference is asserted on every test run. If you change
    one, change both, and let the differential test tell you when they diverge.
    """
    r_s = np.asarray(r_s, float)
    r_I = np.asarray(r_I, float)
    w_s = np.asarray(w_s, float)
    n = len(r_s)
    denom = 1.0 - w_s
    safe = np.where(denom != 0.0, denom, 1.0)
    r_Is = np.where(w_s > 0.0, (r_I - w_s * r_s) / safe, r_I)

    out = [None] * n
    t0 = k + window - 1               # first t whose lo = t-window+1 is >= k
    if t0 >= n:
        return out
    ts = np.arange(t0, n)
    los = ts - window + 1
    m = len(ts)

    S = _win(r_s, window)[los]        # (m, window) views, no copy
    I_win = _win(r_Is, window)[los]
    insync = _rolling_spearman(S, I_win)

    lags = np.arange(-k, k + 1)
    rho = np.full((m, len(lags)), np.nan)
    for j, lag in enumerate(lags):
        if lag == 0:
            rho[:, j] = insync
            continue
        length = window - abs(lag)
        if length < 3:
            continue
        if lag > 0:
            A = _win(r_s, length)[los]          # r_s[lo : t+1-lag]
            B = _win(r_Is, length)[los + lag]   # r_Is[lo+lag : t+1]
        else:
            A = _win(r_s, length)[los - lag]    # r_s[lo-lag : t+1]
            B = _win(r_Is, length)[los]         # r_Is[lo : t+1+lag]
        rho[:, j] = _rolling_spearman(A, B)

    # Best lag. The reference seeds (0, insync) then scans -k..k, replacing
    # only on STRICT improvement. Equivalently: take max |rho|; if |insync|
    # attains it, lag 0 keeps it; otherwise the FIRST lag in scan order that
    # attains it wins. NaN (the reference's None) can never win.
    arho = np.where(np.isnan(rho), -np.inf, np.abs(rho))
    mx = arho.max(axis=1)
    first = np.argmax(arho >= mx[:, None], axis=1)
    best_lag = lags[first]
    best_rho = rho[np.arange(m), first]
    keeps_zero = np.abs(insync) >= mx
    best_lag = np.where(keeps_zero, 0, best_lag)
    best_rho = np.where(keeps_zero, insync, best_rho)

    # beta: np.cov's ddof=1 over ndarray.var()'s ddof=0. That asymmetry is in
    # the reference, so it is reproduced here rather than corrected -- fixing
    # it would silently restate every historical beta_sector.
    dS = S - S.mean(axis=1, keepdims=True)
    dI = I_win - I_win.mean(axis=1, keepdims=True)
    cov01 = np.einsum("ij,ij->i", dS, dI) / (window - 1)
    var_I = np.einsum("ij,ij->i", dI, dI) / window

    floor = 2.0 / (window ** 0.5)
    abs_best = np.abs(best_rho)
    for i in range(m):
        if np.isnan(insync[i]):
            continue
        lag_i = int(best_lag[i])
        if abs_best[i] <= floor:
            cls = "DECOUPLED"
        elif lag_i > 0:
            cls = "LEADER"
        elif lag_i < 0:
            cls = "LAGGARD"
        else:
            cls = "COUPLED"
        out[int(ts[i])] = {
            "insync_corr": float(insync[i]),
            "best_lag": lag_i,
            "lead_lag_corr": float(best_rho[i]),
            "beta_sector": (float(cov01[i] / var_I[i])
                            if var_I[i] > 0.0 else None),
            "n_obs": window,
            "coupling_class": cls,
        }
    return out
