"""Unit tests for the §4.2 regime-input builder (leadlag C'b-1)."""
import numpy as np

from trip_ema.fundamentals.sector_leadlag_inputs import (
    build_regime_leadlag_inputs,
)
from trip_ema.fundamentals.sector_leadlag import compute_sector_leadlag


def test_constituent_weight_is_one_over_n_and_debias_recovers_loo():
    # 4 constituents; this stock is one of them. w_s must be 1/N, and the
    # core debias (r_I - w_s*r_s)/(1 - w_s) must equal the exact equal-weight
    # leave-one-out (N*breadth - regime_s)/(N-1).
    N = 4
    weeks = list(range(60))
    stock = {w: (1.0 if w % 2 else -1.0) for w in weeks}          # std > 0
    breadth = {w: (stock[w] + (-1.0 if w % 3 else 1.0) + 0.0 + 1.0) / N
               for w in weeks}
    n_by = {w: N for w in weeks}
    _, r_s, r_I, w_s, isc = build_regime_leadlag_inputs(
        stock, breadth, n_by, set(weeks),
    )
    assert all(abs(x - 0.25) < 1e-12 for x in w_s)
    assert all(isc)
    r_s_a, r_I_a, w_s_a = map(np.asarray, (r_s, r_I, w_s))
    denom = 1.0 - w_s_a
    safe = np.where(denom != 0.0, denom, 1.0)
    r_Is = np.where(w_s_a > 0.0, (r_I_a - w_s_a * r_s_a) / safe, r_I_a)
    hand_loo = (N * r_I_a - r_s_a) / (N - 1)
    assert np.allclose(r_Is, hand_loo)


def test_non_constituent_and_singleton_weeks_get_zero_weight():
    stock = {0: 1.0, 1: -1.0, 2: 0.0}
    breadth = {0: 0.5, 1: -0.5, 2: 0.0}
    # week 0: constituent but N==1 -> 0 ; week 1: constituent N=5 -> 1/5 ;
    # week 2: not a constituent -> 0
    _, _, _, w_s, isc = build_regime_leadlag_inputs(
        stock, breadth, {0: 1, 1: 5, 2: 5}, {0, 1},
    )
    assert w_s == [0.0, 1 / 5, 0.0]
    assert isc == [True, True, False]


def test_aligns_only_on_weeks_present_in_both():
    stock = {0: 1.0, 1: 1.0, 5: 1.0}
    breadth = {1: 0.2, 5: 0.3, 9: 0.4}
    weeks, r_s, r_I, w_s, isc = build_regime_leadlag_inputs(
        stock, breadth, {}, set(),
    )
    assert weeks == [1, 5]
    assert r_s == [1.0, 1.0]
    assert r_I == [0.2, 0.3]


def test_end_to_end_leader_on_regime_valued_series():
    # A stock whose regime leads the breadth by 2 weeks must classify LEADER
    # at best_lag 2 once fed through the builder + the (Spearman, +/-10) core.
    n, period = 120, 13
    breadth = np.array([1.0 if np.sin(2 * np.pi * i / period) >= 0 else -1.0
                        for i in range(n)])
    stock = np.zeros(n)
    stock[:n - 2] = breadth[2:]                     # stock leads by 2
    sr = {i: float(stock[i]) for i in range(n)}
    br = {i: float(breadth[i]) for i in range(n)}
    _, r_s, r_I, w_s, _ = build_regime_leadlag_inputs(sr, br, {}, set())
    res = compute_sector_leadlag(r_s, r_I, w_s, window=52)
    assert res[-1]["best_lag"] == 2
    assert res[-1]["coupling_class"] == "LEADER"
