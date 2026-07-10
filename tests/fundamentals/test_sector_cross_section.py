"""Unit tests for the cross-sectional rs_rank core (spec §11b)."""
from datetime import datetime, timedelta, timezone

from trip_ema.fundamentals.sector_cross_section import (
    compute_rs_rank_by_week,
)

UTC = timezone.utc


def _weeks(n):
    m0 = datetime(2024, 1, 1, tzinfo=UTC)
    return [m0 + timedelta(weeks=i) for i in range(n)]


def _series(M, step):
    return {M[i]: 100 * ((1 + step) ** i) for i in range(len(M))}


def test_rank_orders_by_relative_performance():
    M = _weeks(12)
    cc = {"A": _series(M, 0.02), "B": _series(M, 0.0), "C": _series(M, -0.01)}
    aw = {t: (None, None) for t in cc}
    r = compute_rs_rank_by_week(cc, aw, window=4)
    w = M[8]
    assert abs(r["A"][w] - 1.0) < 1e-9        # strongest
    assert abs(r["C"][w] - 1 / 3) < 1e-9      # weakest
    assert r["A"][w] > r["B"][w] > r["C"][w]


def test_active_window_excludes_inactive_constituents():
    M = _weeks(12)
    cc = {"A": _series(M, 0.02), "B": _series(M, 0.0), "C": _series(M, -0.01)}
    aw = {"A": (None, None), "B": (None, None), "C": (M[6], None)}
    r = compute_rs_rank_by_week(cc, aw, window=4)
    # week 5: C not yet active -> A ranked among {A, B} only -> 1.0
    assert r["A"][M[5]] == 1.0
    assert M[5] not in r["C"]


def test_warmup_weeks_have_no_rank():
    M = _weeks(12)
    cc = {"A": _series(M, 0.01), "B": _series(M, 0.0)}
    aw = {t: (None, None) for t in cc}
    r = compute_rs_rank_by_week(cc, aw, window=4)
    assert M[2] not in r["A"]                  # < window bars back
    assert M[8] in r["A"]
