"""market_session._EXCHANGE_SPECS must agree with config.yaml's market_hours.

THE PROBLEM NO AST TEST CAN SEE

market_session.py hardcodes a spec per exchange:

    "NSE":  _ExchangeSpec("Asia/Kolkata",     time(9,15), time(15,30), "IN")
    "NYSE": _ExchangeSpec("America/New_York", time(9,30), time(16,0),  "US")

config.yaml declares the same thing:

    market_hours:
      - exchange: NSE
        open:  "09:15"
        close: "15:30"
        tz:    Asia/Kolkata

TWO SOURCES OF TRUTH for every exchange's timezone and session window. The
module's own docstring concedes it: "config.market_hours remains the eventual
single source of truth."

Today they agree, and nothing enforces it. Change the session window in config and
the compute path — volume proration, the week anchor, the daywalk's post-session
instant — silently keeps using the hardcoded spec. No timezone guard catches this:
it is not a naked clock or a hardcoded literal, it is a VALUE MISMATCH between a
Python dict and a YAML file.

A constant duplicated is a constant that drifts. This test is the thing that stops
it — until the spec is genuinely seeded from config, at which point delete this
and the duplication with it.
"""
from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from trip_ema.config.loader import load_config
from trip_ema.domain.market_session import _EXCHANGE_SPECS

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@pytest.mark.skipif(not CONFIG.exists(), reason="config.yaml not present")
def test_exchange_specs_agree_with_config():
    """Every exchange in BOTH places must describe the same market."""
    cfg = load_config(str(CONFIG))
    mismatches: list[str] = []

    for mh in cfg.market_hours:
        spec = _EXCHANGE_SPECS.get(mh.exchange.upper())
        if spec is None:
            # In config but not in the registry: market_session_for() would fall
            # back to a generic 09:00-16:00 UTC session and silently mis-prorate
            # volume for the whole exchange.
            mismatches.append(
                f"{mh.exchange}: in config.market_hours but NOT in "
                f"_EXCHANGE_SPECS - market_session_for() would fall back to a "
                f"generic 09:00-16:00 session in UTC"
            )
            continue

        if spec.tz != mh.tz:
            mismatches.append(
                f"{mh.exchange}.tz: spec={spec.tz!r} config={mh.tz!r}"
            )
        if spec.open_time != _hhmm(mh.open):
            mismatches.append(
                f"{mh.exchange}.open: spec={spec.open_time} config={mh.open}"
            )
        if spec.close_time != _hhmm(mh.close):
            mismatches.append(
                f"{mh.exchange}.close: spec={spec.close_time} config={mh.close}"
            )
        if set(spec.trading_weekdays) != set(mh.weekdays):
            mismatches.append(
                f"{mh.exchange}.weekdays: spec={sorted(spec.trading_weekdays)} "
                f"config={sorted(mh.weekdays)}"
            )

    assert not mismatches, (
        "\n\nmarket_session._EXCHANGE_SPECS has DRIFTED from config.yaml:\n\n  "
        + "\n  ".join(mismatches)
        + "\n\nThe compute path (volume proration, week anchor, post-session "
          "instant)\nuses the SPEC. config.yaml is what an operator edits. When "
          "they\ndisagree, the operator is lying to themselves.\n"
    )


def test_every_configured_exchange_has_a_spec():
    """An exchange you can point the pipeline at must have a session spec.

    Without one, market_session_for() logs a warning and returns a generic
    09:00-16:00 UTC session — which silently mis-prorates volume for every ticker
    on that exchange. A warning in a log is not a gate.
    """
    cfg = load_config(str(CONFIG))
    missing = [
        mh.exchange for mh in cfg.market_hours
        if mh.exchange.upper() not in _EXCHANGE_SPECS
    ]
    assert not missing, (
        f"\n\nConfigured exchanges with no _ExchangeSpec: {missing}\n"
        f"market_session_for() would fall back to a generic 09:00-16:00 UTC "
        f"session.\n"
    )
