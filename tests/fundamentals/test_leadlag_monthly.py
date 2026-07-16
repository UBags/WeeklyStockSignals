"""Tests for the monthly lead/lag recompute (leadlag F1)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import text

from trip_ema.fundamentals.leadlag_monthly import (
    _recomputed_this_month,
    run_monthly_leadlag_recompute,
)
from trip_ema.persistence import transaction

# First Saturday of Feb 2026 is the 7th; the 10th is a Tuesday.
_FIRST_SAT = date(2026, 2, 7)
_NOT_SAT = date(2026, 2, 10)


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, fresh):
        self._fresh = fresh

    def execute(self, sql, params=None):
        return _Res([1] if self._fresh else [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Engine:
    """Fake engine: connect() yields a conn whose freshness gates
    _recomputed_this_month."""
    def __init__(self, fresh):
        self._fresh = fresh

    def connect(self):
        return _Conn(self._fresh)


def _spies():
    calls = []
    return calls, (lambda e, ex: calls.append(("sector", ex)),
                   lambda e, ex: calls.append(("matrix", ex)))


def test_skips_when_not_first_saturday():
    calls, (s, m) = _spies()
    r = run_monthly_leadlag_recompute(
        _Engine(False), "NSE", if_due=True, today=_NOT_SAT,
        sector_fn=s, matrix_fn=m,
    )
    assert not r.ran
    assert "not the first Saturday" in r.reason
    assert calls == []


def test_skips_when_already_recomputed_this_month():
    calls, (s, m) = _spies()
    r = run_monthly_leadlag_recompute(
        _Engine(True), "NSE", if_due=True, today=_FIRST_SAT,
        sector_fn=s, matrix_fn=m,
    )
    assert not r.ran
    assert "already recomputed" in r.reason
    assert calls == []


def test_runs_both_in_order_when_due():
    calls, (s, m) = _spies()
    r = run_monthly_leadlag_recompute(
        _Engine(False), "NSE", if_due=True, today=_FIRST_SAT,
        sector_fn=s, matrix_fn=m,
    )
    assert r.ran
    assert calls == [("sector", "NSE"), ("matrix", "NSE")]  # 6b before 6c


def test_force_runs_regardless_of_date_and_freshness():
    calls, (s, m) = _spies()
    r = run_monthly_leadlag_recompute(
        _Engine(True), "NSE", if_due=False, today=_NOT_SAT,
        sector_fn=s, matrix_fn=m,
    )
    assert r.ran
    assert calls == [("sector", "NSE"), ("matrix", "NSE")]


# ---- DB-backed: the due-gate SQL reads BOTH tables ----

def _seed(conn, table, computed_at):
    if table == "industry_regime_corr":
        conn.execute(text("""
            INSERT INTO industry_regime_corr (
                industry_i, industry_j, "window", asof_date, corr, best_lag,
                lead_lag_corr, n_obs, computed_at
            ) VALUES ('A', 'B', 260, :d, 0.5, 0, 0.5, 100, :ca)
            ON CONFLICT (industry_i, industry_j, "window") DO UPDATE
                SET computed_at = EXCLUDED.computed_at
        """), {"d": computed_at.date(), "ca": computed_at})
    else:
        conn.execute(text("""
            INSERT INTO sector_stock_leadlag (
                ticker_id, asof_date, "window", industry_name, computed_at
            ) VALUES (:tid, :d, 260, 'A', :ca)
            ON CONFLICT (ticker_id, asof_date, "window") DO UPDATE
                SET computed_at = EXCLUDED.computed_at
        """), {"tid": 999001, "d": computed_at.date(), "ca": computed_at})


def test_recomputed_this_month_requires_both_tables_fresh(db_engine):
    today = date(2026, 2, 7)
    this_month = datetime(2026, 2, 3, tzinfo=timezone.utc)
    last_month = datetime(2026, 1, 3, tzinfo=timezone.utc)

    # both stale (last month) -> False
    with transaction(db_engine) as conn:
        conn.execute(text("DELETE FROM industry_regime_corr"))
        conn.execute(text("DELETE FROM sector_stock_leadlag "
                          "WHERE ticker_id = 999001"))
        _seed(conn, "industry_regime_corr", last_month)
        _seed(conn, "sector_stock_leadlag", last_month)
    assert _recomputed_this_month(db_engine, today) is False

    # only the matrix fresh -> still False (needs both)
    with transaction(db_engine) as conn:
        _seed(conn, "industry_regime_corr", this_month)
    assert _recomputed_this_month(db_engine, today) is False

    # both fresh -> True
    with transaction(db_engine) as conn:
        _seed(conn, "sector_stock_leadlag", this_month)
    assert _recomputed_this_month(db_engine, today) is True
