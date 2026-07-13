"""Root test fixtures.

L6-SPECS-ARE-ISOLATED — the exchange registry does not leak between tests.

`trip_ema.domain.market_session._EXCHANGE_SPECS` is a module-global mutable dict,
and `seed_specs_from_config()` — which `load_config()` calls — rewrites it in
place. That is the correct lifecycle for a service: read the operator's config
once, at startup, and let it be the single source of truth for what a market is.

It is a disaster for a test process, which loads many configs, most of which are
minimal fixtures that are entitled to be careless about markets they do not
exercise. One of them declared NYSE in Asia/Kolkata; from the moment that test ran,
every later test in the same xdist worker saw New York trading Indian hours. It
went unnoticed for as long as it existed because nothing had ever asked
market_tz("NYSE") after a config load.

So: restore after EVERY test, not merely after the ones that obviously seed. You
cannot tell from the outside which ones do — load_config() is called all over this
suite, and the mutation is a side effect several layers below the call.
"""
from __future__ import annotations

import pytest

from trip_ema.domain import market_session


@pytest.fixture(autouse=True)
def _isolate_exchange_specs():
    """Snapshot the exchange registry, run the test, put it back."""
    before = dict(market_session._EXCHANGE_SPECS)
    try:
        yield
    finally:
        market_session._EXCHANGE_SPECS.clear()
        market_session._EXCHANGE_SPECS.update(before)
