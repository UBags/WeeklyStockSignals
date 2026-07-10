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
