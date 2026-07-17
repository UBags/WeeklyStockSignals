"""cmd_forward_walk: argument wiring, exit-code policy, and dispatch
registration.

No DB and no threads: the threaded runner is injected via ``run_fn`` and the
exchange is validated against a stub config (the MagicMock seam the sibling CLI
tests use). Nothing here patches a module attribute.
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trip_ema.fundamentals.cli import cmd_forward_walk, register_subparsers
from trip_ema.fundamentals.parallel import FanOutOutcome


@pytest.fixture
def fake_config():
    cfg = MagicMock()
    nse = MagicMock()
    nse.exchange = "NSE"
    cfg.market_hours = [nse]
    return cfg


def _ctx(fake_config):
    return SimpleNamespace(config=fake_config, db_engine=object())


def _args(**kw):
    base = dict(exchange="NSE", tickers=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_returns_0_on_clean_run(fake_config):
    seen = {}

    def fake_run(db_engine, exchange, *, scoped_tickers, progress_cb):
        seen.update(exchange=exchange, scoped_tickers=scoped_tickers)
        return FanOutOutcome(done=3, failed=[])

    rc = cmd_forward_walk(_args(), _ctx(fake_config), run_fn=fake_run)
    assert rc == 0
    assert seen == dict(exchange="NSE", scoped_tickers=None)


def test_returns_1_when_some_tickers_failed(fake_config):
    def fake_run(db_engine, exchange, **kw):
        return FanOutOutcome(done=1, failed=[(9, "Boom: bad")])

    assert cmd_forward_walk(_args(), _ctx(fake_config), run_fn=fake_run) == 1


def test_scopes_tickers_from_csv(fake_config):
    seen = {}

    def fake_run(db_engine, exchange, *, scoped_tickers, progress_cb):
        seen["ids"] = scoped_tickers
        return FanOutOutcome(done=3, failed=[])

    cmd_forward_walk(_args(tickers="10, 20 ,30"), _ctx(fake_config),
                     run_fn=fake_run)
    assert seen["ids"] == [10, 20, 30]


def test_bad_tickers_returns_2_without_running(fake_config):
    called = False

    def fake_run(*a, **k):
        nonlocal called
        called = True
        return FanOutOutcome()

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
        ["forward-walk", "--exchange", "NSE", "--tickers", "10,20"]
    )
    assert ns.exchange == "NSE" and ns.tickers == "10,20"
