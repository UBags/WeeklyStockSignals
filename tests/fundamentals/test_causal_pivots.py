"""Tests for the causal streaming pivot detector (spec v0.3 §5)."""
import pytest

from trip_ema.fundamentals.causal_pivots import CausalPivotTracker


def _run(series, k):
    t = CausalPivotTracker(k=k)
    confirmed = []
    for i, v in enumerate(series):
        r = t.update(i, None if v is None else float(v))
        if r["peak_confirmed"]:
            confirmed.append(("P", r["pivot_bar"], i))
        if r["trough_confirmed"]:
            confirmed.append(("T", r["pivot_bar"], i))
    return t, confirmed


class TestK1:
    SERIES = [10, 11, 12, 13, 12, 11, 10, 11, 13, 15, 14, 13, 12]

    def test_pivots_and_confirmation_lag(self):
        _, conf = _run(self.SERIES, k=1)
        assert conf == [("P", 3, 4), ("T", 6, 7), ("P", 9, 10)]

    def test_last_prev_tracking(self):
        t, _ = _run(self.SERIES, k=1)
        assert (t.last_peak_value, t.last_peak_bar) == (15, 9)
        assert (t.prev_peak_value, t.prev_peak_bar) == (13, 3)
        assert (t.last_trough_value, t.last_trough_bar) == (10, 6)
        assert t.prev_trough_value is None
        assert t.nearest_pivot_bar() == 9


class TestK2:
    def test_broad_peak_confirmed_at_plus_k(self):
        _, conf = _run([1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3], k=2)
        assert ("P", 4, 6) in conf

    def test_strict_rejects_jagged_blip(self):
        # bar2=3 has right-neighbour 3; bar4=3 has left-neighbour 3 -> not strict
        _, conf = _run([1, 2, 3, 2, 3, 2, 1], k=2)
        assert [c for c in conf if c[0] == "P"] == []


class TestNaNWarmup:
    def test_no_pivot_until_clean_window(self):
        seq = [None, None, 10, 11, 12, 13, 12]
        t = CausalPivotTracker(k=1)
        res = [t.update(i, None if v is None else float(v))
               for i, v in enumerate(seq)]
        assert not any(r["peak_confirmed"] or r["trough_confirmed"]
                       for r in res[:6])
        assert res[6]["peak_confirmed"] and res[6]["pivot_bar"] == 5


class TestSnapshotRestore:
    def test_round_trip_carries_k_and_state(self):
        s = [1, 2, 3, 4, 5, 4, 3, 2]
        a = CausalPivotTracker(k=2)
        for i in range(7):
            a.update(i, float(s[i]))
        b = CausalPivotTracker(k=1)
        b.restore(a.snapshot())
        assert b.k == 2
        ra = a.update(7, float(s[7]))
        rb = b.update(7, float(s[7]))
        assert ra == rb
        assert a.last_peak_bar == b.last_peak_bar


def test_k_must_be_positive():
    with pytest.raises(ValueError):
        CausalPivotTracker(k=0)
