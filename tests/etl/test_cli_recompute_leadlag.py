"""Test the recompute-leadlag CLI handler wiring (leadlag F2)."""
from __future__ import annotations

from trip_ema.etl import cli


class _Args:
    exchange = "NSE"
    if_due = True


class _Ctx:
    config = "CFG-SENTINEL"
    db_engine = "ENG-SENTINEL"


class _Report:
    ran = True

    def render(self):
        return "lead/lag recompute — NSE: completed"


def test_cmd_recompute_leadlag_threads_config_and_if_due(monkeypatch):
    captured = {}

    def fake(db_engine, exchange, *, config, if_due):
        captured.update(
            db_engine=db_engine, exchange=exchange,
            config=config, if_due=if_due,
        )
        return _Report()

    monkeypatch.setattr(
        "trip_ema.fundamentals.leadlag_monthly.run_monthly_leadlag_recompute",
        fake,
    )
    rc = cli.cmd_recompute_leadlag(_Args(), _Ctx())
    assert rc == 0
    assert captured == {
        "db_engine": "ENG-SENTINEL",
        "exchange": "NSE",
        "config": "CFG-SENTINEL",
        "if_due": True,
    }
