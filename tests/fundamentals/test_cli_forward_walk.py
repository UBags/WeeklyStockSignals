"""cmd_forward_walk: argument wiring, exit-code policy, and dispatch
registration. No DB and no processes — the orchestrator is injected via
``run_fn`` and exchange validation is isolated, so this is a test of the
handler's own logic and of the dispatch table.
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from trip_ema.fundamentals import cli
from trip_ema.fundamentals.cli import cmd_forward_walk, register_subparsers
from trip_ema.fundamentals.forward_walk_orchestrator import ForwardWalkOutcome


@pytest.fixture(autouse=True)
def _isolate_validate(monkeypatch):
    monkeypatch.setattr(cli, "validate_exchange", lambda ex, cfg: None)


def _ctx():
    return SimpleNamespace(config=object())


def _args(**kw):
    base = dict(exchange="NSE", tickers=None, processes=None, batch_size=500)
    base.update(kw)
    return SimpleNamespace(**base)


def test_returns_0_on_clean_run():
    seen = {}

    def fake_run(config, exchange, *, ticker_ids, processes, batch_size):
        seen.update(exchange=exchange, ticker_ids=ticker_ids,
                    processes=processes, batch_size=batch_size)
        return ForwardWalkOutcome(tickers=3, rows_written=7, failed=[])

    rc = cmd_forward_walk(_args(), _ctx(), run_fn=fake_run)
    assert rc == 0
    assert seen == dict(exchange="NSE", ticker_ids=None,
                        processes=None, batch_size=500)


def test_returns_1_when_some_tickers_failed():
    def fake_run(config, exchange, **kw):
        return ForwardWalkOutcome(tickers=2, rows_written=1,
                                  failed=[(9, "Boom: bad")])

    assert cmd_forward_walk(_args(), _ctx(), run_fn=fake_run) == 1


def test_scopes_tickers_from_csv():
    seen = {}

    def fake_run(config, exchange, *, ticker_ids, processes, batch_size):
        seen["ids"] = ticker_ids
        return ForwardWalkOutcome(tickers=3, rows_written=3, failed=[])

    cmd_forward_walk(_args(tickers="10, 20 ,30"), _ctx(), run_fn=fake_run)
    assert seen["ids"] == [10, 20, 30]


def test_passes_processes_and_batch_size():
    seen = {}

    def fake_run(config, exchange, *, ticker_ids, processes, batch_size):
        seen.update(processes=processes, batch_size=batch_size)
        return ForwardWalkOutcome()

    cmd_forward_walk(_args(processes=4, batch_size=250), _ctx(), run_fn=fake_run)
    assert seen == dict(processes=4, batch_size=250)


def test_bad_tickers_returns_2_without_running():
    called = False

    def fake_run(*a, **k):
        nonlocal called
        called = True
        return ForwardWalkOutcome()

    rc = cmd_forward_walk(_args(tickers="10,oops"), _ctx(), run_fn=fake_run)
    assert rc == 2 and called is False


def test_registration_wires_forward_walk():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()

    def _add_exchange_arg(p):
        p.add_argument("--exchange", required=True)

    table = register_subparsers(sub, _add_exchange_arg)
    assert table.get("forward-walk") is cmd_forward_walk

    ns = parser.parse_args(
        ["forward-walk", "--exchange", "NSE", "--processes", "2",
         "--batch-size", "100"]
    )
    assert ns.exchange == "NSE"
    assert ns.processes == 2 and ns.batch_size == 100
    assert ns.tickers is None
