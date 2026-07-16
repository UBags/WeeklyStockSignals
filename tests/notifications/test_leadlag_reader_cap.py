"""DB-free test: the reader caps corroborators at max_corroborators (G3a)."""
from __future__ import annotations

from trip_ema.notifications.leadlag_reader import load_leadlag_context


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


class _Conn:
    """Returns 5 ensemble members (|corr|-ordered) and empty everything else."""
    def execute(self, textobj, params=None):
        sql = str(textobj)
        if "influential_industry_ensemble" in sql:
            return _Res([
                _Row(member_j=f"I{i}", corr=-0.9 + 0.01 * i, best_lag=i,
                     sign=-1, is_leader=True)
                for i in range(5)
            ])
        return _Res([])  # sector_stock_leadlag + industry_regime_breadth empty


def test_reader_caps_to_max_corroborators():
    assert len(load_leadlag_context(
        _Conn(), 1, "Auto", max_corroborators=2).corroborators) == 2


def test_reader_default_cap_is_three():
    assert len(load_leadlag_context(
        _Conn(), 1, "Auto").corroborators) == 3


def test_reader_cap_above_available_returns_all():
    assert len(load_leadlag_context(
        _Conn(), 1, "Auto", max_corroborators=9).corroborators) == 5
