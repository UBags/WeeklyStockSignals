"""Tests for the V1 SignalEvent dataclass.

Locks in field count, names, immutability, and the SIGNAL_TYPES /
REGIME_VALUES constants. Validation logic lives on the repos
(StockIndicatorStateRepository pattern), not the dataclass.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, UTC

import pytest

from trip_ema.domain.signal_event import (
    REGIME_VALUES,
    SIGNAL_TYPES,
    SignalEvent,
)


def _make(**overrides) -> SignalEvent:
    defaults = dict(
        ticker_id=42,
        timeframe="Weekly",
        bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
        signal_type="BUY",
        regime_before=0,
        regime_after=1,
        price_at_signal=150.0,
        computed_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return SignalEvent(**defaults)


class TestConstants:

    def test_signal_types_tuple(self):
        assert SIGNAL_TYPES == (
            "BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL",
        )
        assert isinstance(SIGNAL_TYPES, tuple)

    def test_regime_values_tuple(self):
        assert REGIME_VALUES == (-1, 0, 1)
        assert isinstance(REGIME_VALUES, tuple)


class TestSignalEvent:

    def test_basic_construction(self):
        s = _make()
        assert s.ticker_id == 42
        assert s.timeframe == "Weekly"
        assert s.signal_type == "BUY"
        assert s.regime_before == 0
        assert s.regime_after == 1
        assert s.price_at_signal == 150.0

    def test_frozen(self):
        s = _make()
        with pytest.raises(FrozenInstanceError):
            s.signal_type = "SELL"  # type: ignore[misc]

    def test_slots_no_undeclared_attrs(self):
        s = _make()
        with pytest.raises((AttributeError, TypeError)):
            s.brand_new = 42  # type: ignore[attr-defined]

    def test_field_count(self):
        """Lock in the 8-field shape: 3 keys + 1 type + 2 regimes +
        1 price + 1 audit. NO signal_id (persistence-internal),
        NO archived_at (historical_signals audit field)."""
        assert len(fields(SignalEvent)) == 8

    def test_expected_field_names(self):
        names = {f.name for f in fields(SignalEvent)}
        expected = {
            "ticker_id", "timeframe", "bar_timestamp",
            "signal_type",
            "regime_before", "regime_after",
            "price_at_signal", "computed_at",
        }
        assert names == expected

    @pytest.mark.parametrize("signal_type", list(SIGNAL_TYPES))
    def test_accepts_all_signal_types(self, signal_type):
        # regime_before/after pairs that match each signal_type's
        # semantics. The dataclass itself doesn't enforce the
        # signal_type ↔ regime relationship — that's the repo's
        # responsibility — but we use realistic pairs here.
        regime_map = {
            "BUY": (0, 1),
            "SELL": (0, -1),
            "EXIT_FROM_BUY": (1, 0),
            "EXIT_FROM_SELL": (-1, 0),
        }
        rb, ra = regime_map[signal_type]
        s = _make(signal_type=signal_type, regime_before=rb,
                  regime_after=ra)
        assert s.signal_type == signal_type
