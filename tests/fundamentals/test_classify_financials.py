"""A3: _classify_financials outcome routing, in isolation.

Injection-only. Fake fetch_fn + fake exception classes + an injected fake
QuarterlyFinancialsRepository that records inserts, plus a no-op db_engine so
the success path's DB writes run without a database. We assert the RETURN
string per branch and the per-quarter insert count; real row effects are the
financials integration suite's job. The RateLimited cool-off path sleeps and
is covered there, not here.
"""

from __future__ import annotations

from datetime import date

import pytest

from trip_ema.fundamentals.runners import _classify_financials


# ---- fakes ----


class _RateLimited(Exception):
    pass


class _Permanent(Exception):
    pass


class _Transient(Exception):
    pass


class _Q:
    """A fake quarter row, matching the attributes insert_snapshot reads."""

    def __init__(self, fq):
        self.fiscal_quarter = fq
        self.revenue = 100.0
        self.ebitda = 20.0
        self.currency = "INR"


class _FakeRepo:
    """Injected in place of QuarterlyFinancialsRepository. Records inserts."""

    inserts: list = []

    def __init__(self, conn):
        self._conn = conn

    def insert_snapshot(self, tid, *, fiscal_quarter, revenue, ebitda,
                        currency, source):
        _FakeRepo.inserts.append((tid, fiscal_quarter, source))


class _FakeResult:
    rowcount = 1

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def scalar(self):
        return None

    def __iter__(self):
        return iter(())


class _NoOpConn:
    def execute(self, *a, **k):
        return _FakeResult()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _NoOpEngine:
    def begin(self):
        return _NoOpConn()

    def connect(self):
        return _NoOpConn()


@pytest.fixture(autouse=True)
def _reset_inserts():
    _FakeRepo.inserts = []
    yield
    _FakeRepo.inserts = []


def _call(fetch_fn):
    return _classify_financials(
        _NoOpEngine(), 1, "SYM", "NSE", 4, fetch_fn,
        _RateLimited, _Permanent, _Transient, _FakeRepo,
    )


# ---- tests ----


class TestClassifyFinancials:

    def test_success_returns_succeeded_and_inserts_each_quarter(self):
        def fetch(s, e, *, max_quarters):
            return [_Q(date(2025, 3, 31)), _Q(date(2025, 6, 30)),
                    _Q(date(2025, 9, 30))]
        assert _call(fetch) == "succeeded"
        assert len(_FakeRepo.inserts) == 3
        assert all(src == "YFINANCE" for _tid, _fq, src in _FakeRepo.inserts)

    def test_success_with_zero_quarters_still_succeeds(self):
        """An empty quarter list is a valid 'we asked and got nothing to
        insert' — still a success (the clear() release fires), no inserts."""
        assert _call(lambda s, e, *, max_quarters: []) == "succeeded"
        assert _FakeRepo.inserts == []

    def test_permanent_returns_failed(self):
        def fetch(s, e, *, max_quarters):
            raise _Permanent("no financials")
        assert _call(fetch) == "failed"

    def test_transient_returns_failed(self):
        def fetch(s, e, *, max_quarters):
            raise _Transient("hiccup")
        assert _call(fetch) == "failed"

    def test_unexpected_returns_failed(self):
        def fetch(s, e, *, max_quarters):
            raise ValueError("boom")
        assert _call(fetch) == "failed"


# NOTE: the RateLimited -> cool-off -> retry path is deliberately NOT unit-
# tested here: _classify_financials sleeps RATE_LIMIT_COOLOFF_S inside that
# branch, and skipping the sleep would require patching time.sleep (forbidden
# by the patch-budget ratchet). The financials integration suite exercises the
# cool-off path end-to-end.
