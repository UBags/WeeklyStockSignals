"""compute_sector_leadlag must equal its scalar reference. Exactly.

This output reaches sector_stock_leadlag -> email enrichment -> ensemble
features. A numerical drift is a wrong trading signal, so the fast path is
diffed against the original scalar implementation on every run rather than
spot-checked once at the time it was written.

The tie-heavy integer case is the one that matters most: the real inputs are
display_regime values (a handful of distinct integers) and breadth (a mean of
those), so almost every window is full of ties. Average-rank handling is
exactly where a plausible-looking reimplementation goes quietly wrong.
"""
from __future__ import annotations

import numpy as np
import pytest

from trip_ema.fundamentals.sector_leadlag import (
    compute_sector_leadlag,
    compute_sector_leadlag_reference,
)

_TOL = 1e-9


def _assert_same(got, want, ctx=""):
    assert len(got) == len(want), f"{ctx}: length {len(got)} != {len(want)}"
    for i, (g, w) in enumerate(zip(got, want, strict=True)):
        where = f"{ctx}[{i}]"
        if w is None or g is None:
            assert g is None and w is None, (
                f"{where}: None mismatch — fast={g!r} ref={w!r}"
            )
            continue
        for key in ("best_lag", "n_obs", "coupling_class"):
            assert g[key] == w[key], (
                f"{where}.{key}: fast={g[key]!r} ref={w[key]!r}"
            )
        for key in ("insync_corr", "lead_lag_corr"):
            assert g[key] == pytest.approx(w[key], abs=_TOL), (
                f"{where}.{key}: fast={g[key]!r} ref={w[key]!r}"
            )
        assert (g["beta_sector"] is None) == (w["beta_sector"] is None), (
            f"{where}.beta_sector: None mismatch"
        )
        if w["beta_sector"] is not None:
            assert g["beta_sector"] == pytest.approx(
                w["beta_sector"], abs=_TOL,
            ), (
                f"{where}.beta_sector: fast={g['beta_sector']!r} "
                f"ref={w['beta_sector']!r}"
            )


def _regimes(rng, n):
    """Realistic inputs: integer regimes, breadth as a coarse mean, and
    self-weights of 1/N on constituent weeks (see sector_leadlag_inputs)."""
    r_s = rng.integers(-3, 4, size=n).astype(float)
    r_I = np.round(rng.normal(size=n), 1)
    w_s = np.where(rng.random(n) < 0.5, 1.0 / rng.integers(2, 20, n), 0.0)
    return r_s, r_I, w_s


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("window", [52, 60])
def test_matches_reference_on_tie_heavy_regimes(seed, window):
    rng = np.random.default_rng(seed)
    r_s, r_I, w_s = _regimes(rng, 300)
    _assert_same(
        compute_sector_leadlag(r_s, r_I, w_s, window),
        compute_sector_leadlag_reference(r_s, r_I, w_s, window),
        f"seed={seed} window={window}",
    )


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_matches_reference_on_continuous_series(seed):
    """Untied floats exercise the rank path's other extreme."""
    rng = np.random.default_rng(seed)
    n = 250
    r_s = rng.normal(size=n)
    r_I = rng.normal(size=n)
    w_s = rng.uniform(0.0, 0.3, size=n)
    _assert_same(
        compute_sector_leadlag(r_s, r_I, w_s, 52),
        compute_sector_leadlag_reference(r_s, r_I, w_s, 52),
        f"seed={seed}",
    )


def test_matches_reference_when_windows_go_constant():
    """Flat stretches make std zero, which the reference reports as None.
    NaN leaking through instead of None would be a silent corruption."""
    rng = np.random.default_rng(21)
    r_s = np.repeat(rng.integers(-1, 2, size=30).astype(float), 10)
    r_I = np.repeat(rng.normal(size=30), 10)
    w_s = np.zeros(300)
    got = compute_sector_leadlag(r_s, r_I, w_s, 52)
    _assert_same(got, compute_sector_leadlag_reference(r_s, r_I, w_s, 52))
    assert any(c is None for c in got), "expected some degenerate windows"


def test_matches_reference_with_zero_self_weight():
    """w_s all zero: the leave-one-out debias is a no-op, r_Is == r_I."""
    rng = np.random.default_rng(31)
    r_s, r_I, _ = _regimes(rng, 200)
    w_s = np.zeros(200)
    _assert_same(
        compute_sector_leadlag(r_s, r_I, w_s, 52),
        compute_sector_leadlag_reference(r_s, r_I, w_s, 52),
    )


def test_series_shorter_than_the_window_yields_all_none():
    rng = np.random.default_rng(41)
    r_s, r_I, w_s = _regimes(rng, 40)
    got = compute_sector_leadlag(r_s, r_I, w_s, 52)
    assert got == [None] * 40
    assert got == compute_sector_leadlag_reference(r_s, r_I, w_s, 52)


def test_exactly_at_the_first_computable_index():
    """t0 = k + window - 1 is a boundary; off-by-one here silently drops or
    invents the first row of every series."""
    rng = np.random.default_rng(51)
    window, k = 52, 10
    n = k + window          # exactly one computable t
    r_s, r_I, w_s = _regimes(rng, n)
    got = compute_sector_leadlag(r_s, r_I, w_s, window)
    want = compute_sector_leadlag_reference(r_s, r_I, w_s, window)
    _assert_same(got, want)
    assert got[k + window - 1] is not None
    assert all(c is None for c in got[:k + window - 1])
