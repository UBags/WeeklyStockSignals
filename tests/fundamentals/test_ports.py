"""Tests for the fundamentals source port and its yfinance adapter.

The adapter's network calls are not exercised here (that would need yfinance
and a live Yahoo). What IS exercised is the one decision the adapter makes on
its own: refusing a call it cannot serve.
"""
from __future__ import annotations

import pytest

from trip_ema.fundamentals.ports import (
    DEFAULT_SOURCE,
    FundamentalsSource,
    SourceCapabilityError,
    YFinanceFundamentalsSource,
)


def test_default_source_is_the_yfinance_adapter():
    assert isinstance(DEFAULT_SOURCE, YFinanceFundamentalsSource)


def test_adapter_satisfies_the_protocol():
    """The adapter is structurally typed against FundamentalsSource — nothing
    forces it to stay that way at import time, so assert it here."""
    assert isinstance(DEFAULT_SOURCE, FundamentalsSource)


def test_missing_get_shares_full_raises_capability_error():
    """yfinance < 0.2.30 has no get_shares_full. Falling back to
    info.sharesOutstanding is not an option — that is a scalar and every caller
    of this method needs a series. Refuse loudly instead of degrading."""

    class AncientTicker:
        pass

    with pytest.raises(SourceCapabilityError, match="0.2.30"):
        YFinanceFundamentalsSource._shares_getter(AncientTicker(), "X.NS")


def test_present_get_shares_full_is_returned_as_is():
    class Ticker:
        def get_shares_full(self, **kw):
            return "series"

    t = Ticker()
    assert YFinanceFundamentalsSource._shares_getter(t, "X.NS")() == "series"
