"""Unit tests for Phase 5-1 domain types.

Locks in the IndicatorState dataclass contract and the
REGIME_VALUES / PATTERN_LABELS constants. Same shape as
test_market_cap_domain.py and test_index_domain.py.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone, UTC

import pytest

from trip_ema.fundamentals import (
    PATTERN_LABELS,
    REGIME_VALUES,
)


class TestAllowedValuesTuples:

    def test_regime_values(self):
        assert REGIME_VALUES == (-1, 0, 1)
        assert isinstance(REGIME_VALUES, tuple)

    def test_pattern_labels(self):
        assert PATTERN_LABELS == (
            "NA",
            "1 SUCCESSIVE HIGHER VALLEY",
            "2 SUCCESSIVE HIGHER VALLEYS",
            "1 SUCCESSIVE LOWER TOP",
            "2 SUCCESSIVE LOWER TOPS",
        )
        assert isinstance(PATTERN_LABELS, tuple)


# ============================================================
# SignalEvent (Phase 6-1)
# ============================================================


from trip_ema.fundamentals import SIGNAL_EVENT_KINDS, SignalEvent


class TestSignalEventConstants:

    def test_signal_event_kinds(self):
        assert SIGNAL_EVENT_KINDS == ("BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL")
        assert isinstance(SIGNAL_EVENT_KINDS, tuple)


class TestSignalEventDataclass:

    def _make(self, **overrides):
        defaults = {
            "ticker_id": 1,
            "timeframe": "1w",
            "trade_date": date(2026, 5, 22),
            "event_kind": "BUY",
            "regime_before": 0,
            "regime_after": 1,
            "close_at_event": 844.27,
            "computed_at": datetime(2026, 5, 22, 16, 0, tzinfo=UTC),
        }
        defaults.update(overrides)
        return SignalEvent(**defaults)

    def test_can_construct_ticker_keyed(self):
        e = self._make(ticker_id=42, timeframe="1w")
        assert e.ticker_id == 42
        assert e.timeframe == "1w"

    def test_frozen(self):
        e = self._make()
        with pytest.raises(FrozenInstanceError):
            e.event_kind = "SELL"  # type: ignore[misc]

    def test_all_event_kinds_accepted(self):
        for k in SIGNAL_EVENT_KINDS:
            e = self._make(event_kind=k)
            assert e.event_kind == k
