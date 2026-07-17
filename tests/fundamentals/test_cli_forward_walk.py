"""cmd_forward_walk: argument wiring, exit-code policy, and dispatch
registration.

No DB and no processes: the orchestrator is injected via ``run_fn`` and the
exchange is validated against a stub config (the same MagicMock seam the sibling
CLI tests use). Nothing here patches a module attribute, so the patch-budget
ratchet stays at zero for this file.
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trip_ema.fundamentals.cli import cmd_forward_walk, register_subparsers
from trip_ema.fundamentals.forward_walk_orchestrator import ForwardWalkOutcome


@pytest.fixture
def fake_config():
    """Minimal stub Config: validate_exchange() reads market_hours via
    configured_exchanges(), so one NSE entry is all the handler needs."""
    cfg = MagicMock()
    nse = MagicMock()
    nse.exchange = "NSE"
    cfg.market_hours = [nse]
    return cfg


def _ctx(fake_config):
    return SimpleNamespace(config=fake_config)


def _args(**kw):
    base = dict(exchange="NSE", tickers=None, processes=None, batch_size=500)
    base.update(kw)
    return SimpleNamespace(**base)


def test_returns_0_on_clean_run(fake_config):
    seen = {}

    def fake_run(config, exchange, *, ticker_ids, processes, batch_size):
        seen.update(exchange=exchange, ticker_ids=ticker_ids,
                    processes=processes, batch_size=batch_size)
        return ForwardWalkOutcome(tickers=3, rows_written=7, failed=[])

    rc = cmd_forward_walk(_args(), _ctx(fake_config), run_fn=fake_run)
    assert rc == 0
    assert seen == dict(exchange="NSE", ticker_ids=None,
                        processes=None, batch_size=500)


def test_returns_1_when_some_tickers_failed(fake_config):
    def fake_run(config, exchange, **kw):
        return ForwardWalkOutcome(tickers=2, rows_written=1,
                                  failed=[(9, "Boom: bad")])

    assert cmd_forward_walk(_args(), _ctx(fake_config), run_fn=fake_run) == 1


def test_scopes_tickers_from_csv(fake_config):
    seen = {}

    def fake_run(config, exchange, *, ticker_ids, processes, batch_size):
        seen["ids"] = ticker_ids
        return ForwardWalkOutcome(tickers=3, rows_written=3, failed=[])

    cmd_forward_walk(_args(tickers="10, 20 ,30"), _ctx(fake_config),
                     run_fn=fake_run)
    assert seen["ids"] == [10, 20, 30]


def test_passes_processes_and_batch_size(fake_config):
    seen = {}

    def fake_run(config, exchange, *, ticker_ids, processes, batch_size):
        seen.update(processes=processes, batch_size=batch_size)
        return ForwardWalkOutcome()

    cmd_forward_walk(_args(processes=4, batch_size=250), _ctx(fake_config),
                     run_fn=fake_run)
    assert seen == dict(processes=4, batch_size=250)


def test_bad_tickers_returns_2_without_running(fake_config):
    called = False

    def fake_run(*a, **k):
        nonlocal called
        called = True
        return ForwardWalkOutcome()

    rc = cmd_forward_walk(_args(tickers="10,oops"), _ctx(fake_config),
                          run_fn=fake_run)
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
