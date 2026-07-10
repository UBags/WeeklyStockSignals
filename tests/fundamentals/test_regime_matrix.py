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
