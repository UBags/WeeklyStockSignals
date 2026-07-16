"""A2: _classify_shares outcome routing, in isolation.

Injection-only. Fake fetch_fn drives each branch; fake exception classes let
us trigger RateLimited/Permanent/Transient without importing the real ones;
a no-op db_engine lets the DB-writing branches run without a database. We
assert the RETURN string ('succeeded' | 'failed') per branch — the real row
effects are the shares integration suite's job.
"""

from __future__ import annotations

from datetime import date

import pytest

from trip_ema.fundamentals.runners import _classify_shares


# ---- fakes ----


class _RateLimited(Exception):
    pass


class _Permanent(Exception):
    pass


class _Transient(Exception):
    pass


class _Info:
    def __init__(self, shares=1000, source="YFINANCE"):
        self.shares_outstanding = shares
        self.source = source


class _FakeResult:
    """Permissive stand-in for a SQLAlchemy Result. The repos read attributes
    like .rowcount off execute()'s return; anything unspecified yields a
    harmless default so the success path doesn't trip the bare except."""

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


def _call(fetch_fn):
    return _classify_shares(
        _NoOpEngine(), 1, "SYM", "NSE", date(2026, 1, 1), fetch_fn,
        _RateLimited, _Permanent, _Transient,
    )


# ---- tests ----


class TestClassifyShares:

    def test_success_returns_succeeded(self):
        assert _call(lambda s, e: _Info()) == "succeeded"

    def test_permanent_returns_failed(self):
        def fetch(s, e):
            raise _Permanent("no shares data")
        assert _call(fetch) == "failed"

    def test_transient_returns_failed(self):
        def fetch(s, e):
            raise _Transient("hiccup")
        assert _call(fetch) == "failed"

    def test_unexpected_returns_failed(self):
        def fetch(s, e):
            raise ValueError("boom")
        assert _call(fetch) == "failed"


# NOTE: the RateLimited -> cool-off -> retry path is deliberately NOT unit-
# tested here: _classify_shares sleeps RATE_LIMIT_COOLOFF_S inside that branch,
# and the only ways to avoid the sleep in a unit test are to patch time.sleep
# (forbidden by the patch-budget ratchet) or to thread a sleep seam through the
# worker purely for the test. The shares integration suite exercises the
# cool-off path end-to-end instead, where the sleep is acceptable.
