"""A permanent verdict must survive three asks, not one.

fundamentals_fetch_status can only ever hold UNAVAILABLE, and it has no
expiry. On 2026-07-18 one throttled pass wrote 2,059 EQ tickers -- ITC, LT,
PNB, LUPIN, COFORGE among them -- every row at attempts=1, and nothing in the
schema could tell "asked once mid-throttle" from "asked and answered".

The spine now re-asks L1's failures serially, twice. Workers need no change:
success clears the row, permanent bumps attempts. So attempts=1 becomes
unreachable, and attempts=3 means three spaced asks at three moments.

Both DB touchpoints are injected (checkpoint_sink, close_sink), so these run
with no database and no monkeypatch.
"""
from __future__ import annotations

import threading

from trip_ema.fundamentals.runners import _run_reference_sweep


def _tickers(n):
    return [{"id": i, "symbol": f"SYM{i}"} for i in range(1, n + 1)]


class _Worker:
    """Scripted outcome per (ticker, attempt), recording which thread ran each
    call so the serial layers can be told apart from the threaded one."""

    def __init__(self, script):
        self.script = script
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, t):
        with self._lock:
            n = sum(1 for s, _ in self.calls if s == t["symbol"])
            self.calls.append((t["symbol"], threading.get_ident()))
        seq = self.script.get(t["symbol"], ["failed"])
        return seq[min(n, len(seq) - 1)]

    def attempts(self, symbol):
        return sum(1 for s, _ in self.calls if s == symbol)


def _run(worker, tickers, *, retry_layers=2, retry_gap_s=0.0):
    closes = []
    _run_reference_sweep(
        None, run_id=1, exchange="NSE", phase="financials",
        ticker_list=tickers, completed_ids=set(),
        worker_fn=worker, max_workers=2,
        retry_gap_s=retry_gap_s, retry_layers=retry_layers,
        checkpoint_sink=lambda *a, **k: None,
        close_sink=lambda _e, _r, **kw: closes.append(kw),
    )
    return closes


def test_a_failure_is_asked_three_times_in_total():
    """L1 plus two serial layers. attempts=1 must be unreachable."""
    w = _Worker({"SYM1": ["failed"]})
    _run(w, _tickers(1))
    assert w.attempts("SYM1") == 3


def test_a_success_is_asked_once():
    w = _Worker({"SYM1": ["succeeded"]})
    _run(w, _tickers(1))
    assert w.attempts("SYM1") == 1


def test_recovery_in_l2_stops_further_asks_and_moves_the_counters():
    w = _Worker({"SYM1": ["failed", "succeeded"]})
    closes = _run(w, _tickers(1))
    assert w.attempts("SYM1") == 2
    assert closes[-1]["succeeded"] == 1
    assert closes[-1]["failed"] == 0


def test_recovery_in_l3_is_still_counted():
    w = _Worker({"SYM1": ["failed", "failed", "succeeded"]})
    closes = _run(w, _tickers(1))
    assert w.attempts("SYM1") == 3
    assert closes[-1]["succeeded"] == 1
    assert closes[-1]["failed"] == 0


def test_a_persistent_failure_stays_failed():
    w = _Worker({"SYM1": ["failed"]})
    closes = _run(w, _tickers(1))
    assert closes[-1]["failed"] == 1
    assert closes[-1]["succeeded"] == 0


def test_only_failures_are_retried():
    w = _Worker({
        "SYM1": ["succeeded"],
        "SYM2": ["failed"],
        "SYM3": ["succeeded"],
    })
    _run(w, _tickers(3))
    assert w.attempts("SYM1") == 1
    assert w.attempts("SYM3") == 1
    assert w.attempts("SYM2") == 3


def test_retry_layers_run_on_the_calling_thread():
    """These retries exist because the provider already refused once. Fanning
    them out would rebuild the burst that caused the refusal."""
    w = _Worker({"SYM1": ["failed"], "SYM2": ["failed"]})
    _run(w, _tickers(2))
    retry_threads = {ident for _s, ident in w.calls[2:]}
    assert retry_threads == {threading.get_ident()}, retry_threads


def test_retry_layers_zero_preserves_the_old_behaviour():
    w = _Worker({"SYM1": ["failed"]})
    _run(w, _tickers(1), retry_layers=0)
    assert w.attempts("SYM1") == 1


def test_skipped_in_a_retry_moves_out_of_failed():
    w = _Worker({"SYM1": ["failed", "skipped"]})
    closes = _run(w, _tickers(1))
    assert closes[-1]["skipped"] == 1
    assert closes[-1]["failed"] == 0
