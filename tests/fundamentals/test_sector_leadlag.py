"""Unit tests for the §16 sector lead/lag compute."""
import numpy as np

from trip_ema.fundamentals.sector_leadlag import compute_sector_leadlag


def test_stock_leading_sector_is_leader():
    rng = np.random.default_rng(0)
    n = 120
    sector = rng.normal(0, 0.02, n)
    stock = np.zeros(n)
    stock[:n - 2] = sector[2:]          # stock leads sector by 2
    stock += rng.normal(0, 0.003, n)
    res = compute_sector_leadlag(stock, sector, np.zeros(n), window=52)
    assert res[-1]["best_lag"] == 2
    assert res[-1]["coupling_class"] == "LEADER"
    assert res[-1]["n_obs"] == 52


def test_contemporaneous_is_coupled():
    rng = np.random.default_rng(1)
    n = 120
    sector = rng.normal(0, 0.02, n)
    stock = sector + rng.normal(0, 0.001, n)
    res = compute_sector_leadlag(stock, sector, np.zeros(n), window=52)
    assert res[-1]["best_lag"] == 0
    assert res[-1]["coupling_class"] == "COUPLED"


def test_insufficient_history_is_none():
    r = compute_sector_leadlag(
        np.zeros(10), np.zeros(10), np.zeros(10), window=52,
    )
    assert all(x is None for x in r)


def test_debias_shifts_constituent_correlation():
    rng = np.random.default_rng(2)
    n = 120
    sector = rng.normal(0, 0.02, n)
    stock = sector * 0.5 + rng.normal(0, 0.01, n)
    nonc = compute_sector_leadlag(stock, sector, np.zeros(n), window=52)[-1]
    cons = compute_sector_leadlag(
        stock, sector, np.full(n, 0.3), window=52,
    )[-1]
    assert abs(nonc["insync_corr"] - cons["insync_corr"]) > 1e-6


# ---- leadlag C'a parity (Spearman + wide-lag) ----


def test_wide_lag_detects_far_lead_beyond_old_k():
    # A 7-week lead is only reachable with the widened +/-10 scan; old _K=4
    # capped best_lag at 4. This test fails unless _K widened.
    rng = np.random.default_rng(5)
    n = 120
    sector = rng.normal(0, 0.02, n)
    stock = np.zeros(n)
    stock[:n - 7] = sector[7:]          # stock leads sector by 7
    stock += rng.normal(0, 0.003, n)
    res = compute_sector_leadlag(stock, sector, np.zeros(n), window=52)
    assert res[-1]["best_lag"] == 7
    assert res[-1]["coupling_class"] == "LEADER"
    assert res[-1]["n_obs"] == 52


def test_spearman_leader_on_regime_like_series():
    # Preview of the C'b input: feed a -1/0/+1 regime-valued series. A clean
    # 2-week lead must classify LEADER at best_lag 2 under Spearman.
    n = 120
    period = 13
    sector = np.array([1.0 if np.sin(2 * np.pi * i / period) >= 0 else -1.0
                       for i in range(n)])
    stock = np.zeros(n)
    stock[:n - 2] = sector[2:]          # stock leads sector by 2
    res = compute_sector_leadlag(stock, sector, np.zeros(n), window=52)
    assert res[-1]["best_lag"] == 2
    assert res[-1]["coupling_class"] == "LEADER"
