"""send_signal_report wires the lead/lag clause + threads the cap (leadlag G3b).

No monkeypatch: _make_leadlag_fn is exercised with a fake connection (a test
double), driving the REAL reader — so the patch-budget guard stays happy.
"""
from __future__ import annotations

from trip_ema.notifications.signal_report import _make_leadlag_fn


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _Row:
    def __init__(self, **k):
        self.__dict__.update(k)


class _FakeConn:
    """5 ensemble members (|corr|-ordered); everything else empty."""
    def execute(self, textobj, params=None):
        if "influential_industry_ensemble" in str(textobj):
            return _Res([
                _Row(member_j=f"I{i}", corr=-0.9 + 0.01 * i, best_lag=i,
                     sign=-1, is_leader=True)
                for i in range(5)
            ])
        return _Res([])


class _SignalRow:
    def __init__(self, industry):
        self.ticker_id = 7
        self.industry = industry


def test_leadlag_fn_threads_cap_to_reader():
    fn = _make_leadlag_fn(_FakeConn(), max_corroborators=2)
    ctx = fn(_SignalRow("Automobiles"))
    assert ctx is not None
    assert len(ctx.corroborators) == 2      # cap threaded to the reader


def test_leadlag_fn_default_cap():
    fn = _make_leadlag_fn(_FakeConn(), max_corroborators=3)
    assert len(fn(_SignalRow("Automobiles")).corroborators) == 3


def test_leadlag_fn_none_when_no_industry():
    fn = _make_leadlag_fn(_FakeConn(), max_corroborators=3)
    assert fn(_SignalRow(None)) is None     # reader not consulted
