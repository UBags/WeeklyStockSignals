"""The exchange registry is global, mutable, and must not leak.

These tests exist because a config fixture rewrote NYSE's timezone to
Asia/Kolkata for an entire worker process and the suite stayed green for however
long that fixture has existed. Green because nothing asked. The anchor tests ask.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from trip_ema.domain import market_session
from trip_ema.domain.market_session import (
    market_tz,
    reset_specs_to_defaults,
)


class TestSpecRegistryIsolation:

    def test_new_york_is_in_new_york(self):
        """The canary. If a fixture has poisoned the registry, this is what says
        so — and it says so in one line instead of in a mystifying anchor
        assertion three packages away."""
        assert market_tz("NYSE") == ZoneInfo("America/New_York")
        assert market_tz("NSE") == ZoneInfo("Asia/Kolkata")
        assert market_tz("LSE") == ZoneInfo("Europe/London")

    def test_seeding_mutates_and_reset_restores(self):
        """The mutation is real — that is the hazard — and it now has an undo."""
        original = market_tz("NYSE")

        market_session._EXCHANGE_SPECS["NYSE"] = market_session._ExchangeSpec(
            "Asia/Kolkata", market_session.time(9, 15),
            market_session.time(15, 30), "IN",
        )
        assert market_tz("NYSE") == ZoneInfo("Asia/Kolkata")   # poisoned

        reset_specs_to_defaults()
        assert market_tz("NYSE") == original                    # and back

    def test_the_autouse_fixture_cleaned_up_after_the_previous_test(self):
        """Ordering-sensitive on purpose: the test above deliberately poisons the
        registry. If the autouse fixture in tests/conftest.py is not doing its job,
        this fails."""
        assert market_tz("NYSE") == ZoneInfo("America/New_York")

    def test_unknown_exchange_still_raises(self):
        """Containment must not have quietly reintroduced a fallback. There is no
        safe default for a market you cannot name."""
        with pytest.raises(market_session.UnknownExchangeError):
            market_tz("MOON")
