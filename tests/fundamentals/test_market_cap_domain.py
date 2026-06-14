"""Unit tests for Phase 4A domain types.

These exist mainly to lock in the MARKET_CAP_SOURCES contract and
confirm the dataclass shape (frozen + slots) — the same shape we
use for IndustryClassification, SharesOutstanding, and
QuarterlyFinancial. If a future refactor breaks frozen-ness or
adds a non-tuple value, these tests catch it.

The repository, fetcher, and runner tests are in separate files
(coming next session) and exercise the full database round-trip.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone

import pytest

from trip_ema.fundamentals import (
    MARKET_CAP_SOURCES,
    DailyMarketCap,
)


class TestMarketCapSources:

    def test_has_expected_values(self):
        """The values are part of the data layer contract — the
        migration's CHECK constraint expects exactly these strings.
        Locking in here means a future drift between code and DB
        is caught at test time rather than at INSERT time."""
        assert MARKET_CAP_SOURCES == (
            "COMPUTED",
            "YFINANCE_HIST_MCAP",
            "APPROX_TODAYS_SHARES",
            "MANUAL_OVERRIDE",
        )

    def test_is_a_tuple(self):
        """Mutating SHARES_SOURCES at runtime would be a quiet
        disaster — keep it immutable."""
        assert isinstance(MARKET_CAP_SOURCES, tuple)


class TestDailyMarketCapDataclass:

    def _make(self, **overrides):
        defaults = {
            "ticker_id": 1,
            "trade_date": date(2026, 5, 22),
            "close_price": 1450.5,
            "shares_used": 6_700_000_000,
            "market_cap": 9_711_350_000_000.0,
            "source": "COMPUTED",
            "computed_at": datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        }
        defaults.update(overrides)
        return DailyMarketCap(**defaults)

    def test_can_construct(self):
        m = self._make()
        assert m.market_cap == 9_711_350_000_000.0
        assert m.source == "COMPUTED"
        assert m.shares_used == 6_700_000_000

    def test_frozen(self):
        """Same frozen pattern as the other Phase 1-3 dataclasses."""
        m = self._make()
        with pytest.raises(FrozenInstanceError):
            m.market_cap = 0.0  # type: ignore[misc]

    def test_slots(self):
        """Defensive: catch typos like `m.markt_cap` at construction
        rather than as a silent attribute assignment.

        Frozen+slots raises a confusing-looking TypeError for
        undefined fields (vs FrozenInstanceError for defined ones).
        What we care about is that assignment fails — either
        exception is fine."""
        m = self._make()
        with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
            m.not_a_real_field = 1  # type: ignore[attr-defined]

    def test_shares_used_can_be_None(self):
        """For source=YFINANCE_HIST_MCAP, shares_used is unknown —
        yfinance gives us the mcap directly. NULL is meaningful."""
        m = self._make(shares_used=None, source="YFINANCE_HIST_MCAP")
        assert m.shares_used is None

    def test_zero_market_cap_allowed_in_dataclass(self):
        """The migration's CHECK constraint forbids negative market
        cap, but zero is allowed (newly-listed company, day before
        first trade). The dataclass doesn't replicate the DB
        constraint — that's the DB's job."""
        m = self._make(market_cap=0.0)
        assert m.market_cap == 0.0
