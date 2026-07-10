"""Cross-sectional rs_rank core (spec §11b, coverage b)."""
from datetime import datetime, timedelta, timezone

from trip_ema.fundamentals.sector_cross_section import compute_rs_rank_by_week

UTC = timezone.utc


def _weeks(n):
    m0 = datetime(2024, 1, 1, tzinfo=UTC)
    return [m0 + timedelta(weeks=i) for i in range(n)]


def _series(M, step):
    return {M[i]: 100 * ((1 + step) ** i) for i in range(len(M))}


def test_ranks_all_stocks_against_constituent_reference():
    M = _weeks(12)
    ref = {"A": _series(M, 0.02), "B": _series(M, 0.0),
           "C": _series(M, -0.01)}
    ranked = {**ref, "X": _series(M, 0.05), "Y": _series(M, 0.01)}
    aw = {t: (None, None) for t in ref}
    r = compute_rs_rank_by_week(ranked, ref, aw, window=4)
    w = M[8]
    assert abs(r["A"][w] - 1.0) < 1e-9
    assert abs(r["C"][w] - 1 / 3) < 1e-9
    assert abs(r["X"][w] - 1.0) < 1e-9      # non-constituent beats all
    assert "X" not in ref                    # ranked, not reference


def test_active_window_limits_reference_set():
    M = _weeks(12)
    ref = {"A": _series(M, 0.02), "B": _series(M, 0.0),
           "C": _series(M, -0.01)}
    ranked = {**ref, "X": _series(M, 0.05)}
    aw = {"A": (None, None), "B": (None, None), "C": (M[6], None)}
    r = compute_rs_rank_by_week(ranked, ref, aw, window=4)
    assert abs(r["X"][M[5]] - 1.0) < 1e-9    # vs {A, B} only


def test_warmup_weeks_have_no_rank():
    M = _weeks(12)
    ref = {"A": _series(M, 0.01), "B": _series(M, 0.0)}
    aw = {t: (None, None) for t in ref}
    r = compute_rs_rank_by_week(ref, ref, aw, window=4)
    assert M[2] not in r["A"]
    assert M[8] in r["A"]
