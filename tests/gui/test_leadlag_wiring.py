"""Unit test for the SignalsView lead/lag resolution helper (leadlag E2).

DB-free: drives the module-level _resolve_leadlag_context with a fake conn.
Lives under tests/gui because importing signals.py pulls in PySide6.
"""
from __future__ import annotations

from trip_ema.gui.views.signals import _resolve_leadlag_context


class _Row:
    def __init__(self, **k):
        self.__dict__.update(k)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _Conn:
    def __init__(self, tid_row, leadlag=None):
        self._tid = tid_row
        self._ll = leadlag or {}

    def execute(self, textobj, params=None):
        sql = str(textobj)
        if "FROM tickers" in sql:
            return _Result([self._tid] if self._tid else [])
        if "sector_stock_leadlag" in sql:
            return _Result(self._ll.get("stock", []))
        if "influential_industry_ensemble" in sql:
            return _Result(self._ll.get("ens", []))
        if "industry_regime_breadth" in sql:
            return _Result(self._ll.get("breadth", []))
        return _Result([])


def test_resolves_context_when_ticker_and_industry_known():
    tid = _Row(ticker_id=7, industry="Automobiles")
    ll = {
        "stock": [_Row(best_lag=-2, lead_lag_corr=0.71,
                       coupling_class="LAGGARD")],
        "ens": [],
        "breadth": [_Row(industry_name="Automobiles", breadth=b)
                    for b in (1.0, 1.0, -1.0)],
    }
    ctx, industry = _resolve_leadlag_context(
        _Conn(tid, ll), "NSE", "TATA", "EQ",
    )
    assert industry == "Automobiles"
    assert ctx.stock_coupling == "LAGGARD"
    assert ctx.stock_best_lag == -2
    assert ctx.industry_regime_now == -1     # sign of latest breadth


def test_returns_none_when_ticker_missing():
    assert _resolve_leadlag_context(_Conn(None), "NSE", "X", "EQ") == (None, None)


def test_returns_none_when_industry_missing():
    tid = _Row(ticker_id=7, industry=None)
    assert _resolve_leadlag_context(_Conn(tid), "NSE", "X", "EQ") == (None, None)
