"""Unit tests for the §17 industry regime correlation matrix."""
from datetime import datetime, timedelta, timezone

import numpy as np

from trip_ema.fundamentals.regime_matrix import compute_regime_matrix

UTC = timezone.utc


def _weeks(n):
    return [datetime(2020, 1, 6, tzinfo=UTC) + timedelta(weeks=i)
            for i in range(n)]


def test_leading_industry_has_negative_best_lag():
    w = _weeks(300)
    rng = np.random.default_rng(7)
    market = np.tanh(np.cumsum(rng.normal(0, 0.3, 300)) / 5)
    inds = {}
    for k in range(30):
        beta = rng.uniform(0.3, 1.0)
        idio = np.tanh(np.cumsum(rng.normal(0, 0.2, 300)) / 5)
        inds[f"I{k:02d}"] = {
            w[i]: beta * market[i] + 0.5 * idio[i] for i in range(300)
        }
    drv = np.tanh(np.cumsum(rng.normal(0, 0.25, 300)) / 5)
    inds["I00"] = {w[i]: 0.6 * market[i] + drv[i - 2] for i in range(2, 300)}
    inds["I01"] = {w[i]: 0.6 * market[i] + drv[i] for i in range(300)}
    rows, _ = compute_regime_matrix(inds, window=260)
    d = {(i, j): bl for (i, j, c, bl, llc, n) in rows}
    assert d[("I00", "I01")] < 0        # I01 leads I00
    assert d[("I01", "I00")] > 0        # mirror


def test_too_few_weeks_returns_empty():
    w = _weeks(2)
    rows, _ = compute_regime_matrix(
        {"A": {w[0]: 0.1, w[1]: 0.2}, "B": {w[0]: 0.0, w[1]: 0.1}},
        window=260,
    )
    assert rows == []


# ---- leadlag A' parity (Spearman + wide-lag) ----


def test_spearman_matrix_still_populates_and_sign_holds():
    w = _weeks(300)
    rng = np.random.default_rng(7)
    market = np.tanh(np.cumsum(rng.normal(0, 0.3, 300)) / 5)
    inds = {}
    for k in range(30):
        beta = rng.uniform(0.3, 1.0)
        idio = np.tanh(np.cumsum(rng.normal(0, 0.2, 300)) / 5)
        inds[f"I{k:02d}"] = {w[i]: beta * market[i] + 0.5 * idio[i]
                             for i in range(300)}
    drv = np.tanh(np.cumsum(rng.normal(0, 0.25, 300)) / 5)
    inds["I00"] = {w[i]: 0.6 * market[i] + drv[i - 2] for i in range(2, 300)}
    inds["I01"] = {w[i]: 0.6 * market[i] + drv[i] for i in range(300)}
    rows, _ = compute_regime_matrix(inds, window=260)
    assert rows  # populates under Spearman
    d = {(i, j): bl for (i, j, c, bl, llc, n) in rows}
    assert d[("I00", "I01")] < 0        # I01 still leads I00
    assert d[("I01", "I00")] > 0


def test_wide_lag_detects_far_lead_beyond_old_k():
    # A 7-week lead is only reachable with the widened +/-10 scan (old _K=4
    # capped at +/-4). If _K did not widen, best_lag saturates at <= 4.
    w = _weeks(320)
    rng = np.random.default_rng(11)
    market = np.tanh(np.cumsum(rng.normal(0, 0.3, 320)) / 5)
    inds = {}
    for k in range(12):
        idio = np.tanh(np.cumsum(rng.normal(0, 0.2, 320)) / 5)
        inds[f"F{k:02d}"] = {w[i]: 0.5 * market[i] + 0.5 * idio[i]
                             for i in range(320)}
    drv = np.tanh(np.cumsum(rng.normal(0, 0.25, 320)) / 5)
    inds["LEAD"] = {w[i]: 0.4 * market[i] + drv[i] for i in range(320)}
    inds["LAG"] = {w[i]: 0.4 * market[i] + drv[i - 7] for i in range(7, 320)}
    rows, _ = compute_regime_matrix(inds, window=300)
    d = {(i, j): bl for (i, j, c, bl, llc, n) in rows}
    assert d[("LEAD", "LAG")] > 4       # requires the widened scan
    assert d[("LEAD", "LAG")] <= 10


def test_spearman_helper_beats_pearson_on_monotone_nonlinear():
    # Spearman sees a perfect monotone (nonlinear) relationship that Pearson
    # underrates; and _rankdata averages tied ranks.
    from trip_ema.fundamentals.regime_matrix import _rankdata, _spearman
    rng = np.random.default_rng(9)
    x = np.sort(rng.normal(0, 1, 200))
    y = np.sign(x) * (x ** 2)                    # strictly monotone in x
    rho_s = _spearman(x, y)
    rho_p = float(np.corrcoef(x, y)[0, 1])
    assert rho_s is not None and rho_s > 0.999   # rank-perfect
    assert rho_s > rho_p                          # Spearman > Pearson here
    r = _rankdata(np.array([10.0, 10.0, 20.0, 30.0]))
    assert list(r) == [0.5, 0.5, 2.0, 3.0]        # average tie ranks
