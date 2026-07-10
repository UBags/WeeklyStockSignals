"""Unit tests for §17.4 ensemble selection (3 variants)."""
from trip_ema.fundamentals.ensemble import compute_ensembles


def test_variants_apply_different_thresholds():
    n = 260
    rows = [
        ("A", "B", 0.40, -2, 0.55, n),
        ("A", "C", -0.30, 1, -0.35, n),
        ("A", "D", 0.14, 0, 0.14, n),
        ("A", "E", 0.05, 0, 0.05, n),
    ]
    by: dict = {}
    for (i, j, v, c, bl, isl, sg) in compute_ensembles(rows):
        by.setdefault(v, set()).add(j)
    assert "D" in by["floor"]        # 0.14 >= 2/sqrt(260) = 0.124
    assert "D" not in by["mid"]      # 0.14 < 0.15
    assert by["strict"] == {"B", "C"}
    assert "E" not in by["floor"]


def test_leader_and_sign_flags():
    n = 260
    rows = [("A", "B", 0.40, -2, 0.55, n), ("A", "C", -0.30, 1, -0.35, n)]
    res = {
        (j, v): (isl, sg)
        for (i, j, v, c, bl, isl, sg) in compute_ensembles(rows)
    }
    assert res[("B", "mid")] == (True, 1)
    assert res[("C", "mid")] == (False, -1)


def test_cap_limits_members():
    n = 260
    rows = [("A", f"M{k:02d}", 0.30 + 0.01 * k, 0, 0.3, n)
            for k in range(15)]
    res = compute_ensembles(rows)
    assert len([j for (i, j, v, *_) in res if v == "strict"]) == 8
    assert len([j for (i, j, v, *_) in res if v == "floor"]) == 10
