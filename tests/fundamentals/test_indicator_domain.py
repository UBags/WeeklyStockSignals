"""Unit tests for Phase 5-1 domain types.

Locks in the IndicatorState dataclass contract and the
REGIME_VALUES / PATTERN_LABELS constants. Same shape as
test_market_cap_domain.py and test_index_domain.py.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone

import pytest

from trip_ema.fundamentals import (
    PATTERN_LABELS,
    REGIME_VALUES,
    IndicatorState,
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


class TestIndicatorStateDataclass:

    def _make(self, **overrides):
        """Helper. Returns an IndicatorState with a 'ticker'-shaped
        identity by default; pass industry_name (and clear
        ticker_id/timeframe) to get an 'industry'-shaped one."""
        defaults = {
            "ticker_id": 1,
            "timeframe": "1w",
            "industry_name": None,
            "trade_date": date(2026, 5, 22),
            "med_ema": 1500.0,
            "long_ema": 1450.0,
            "longest_ema": 1400.0,
            "atr": 25.0,
            "upper_inner_band": 1505.0,
            "lower_inner_band": 1395.0,
            "upper_mid_band": 1550.0,
            "lower_mid_band": 1350.0,
            "upper_outer_band": 1612.5,
            "lower_outer_band": 1287.5,
            "display_regime": 1,
            "pierces_upper_mid": False,
            "pierces_lower_mid": False,
            "dist_to_med_in_hl_units": 0.5,
            "dist_to_long_in_hl_units": 1.2,
            "dist_to_longest_in_hl_units": 2.0,
            "mean_dist_to_med_in_hl_units": 0.7,
            "mean_dist_to_long_in_hl_units": 1.5,
            "mean_dist_to_longest_in_hl_units": 2.5,
            "bars_since_med_turn": 5,
            "bars_since_long_turn": 12,
            "bars_since_longest_turn": 30,
            "pct_change_since_med_turn": 3.5,
            "pct_change_since_long_turn": 8.0,
            "pct_change_since_longest_turn": 15.0,
            "med_pattern_label": "NA",
            "long_pattern_label": "NA",
            "longest_pattern_label": "NA",
            "med_turn_buy": False,
            "med_turn_sell": False,
            "long_turn_buy": False,
            "long_turn_sell": False,
            "longest_turn_buy": False,
            "longest_turn_sell": False,
            "computed_at": datetime(2026, 5, 22, 16, 0, tzinfo=timezone.utc),
        }
        defaults.update(overrides)
        return IndicatorState(**defaults)

    def test_can_construct_ticker_keyed(self):
        s = self._make()
        assert s.ticker_id == 1
        assert s.timeframe == "1w"
        assert s.industry_name is None

    def test_can_construct_industry_keyed(self):
        s = self._make(
            ticker_id=None, timeframe=None,
            industry_name="IT - Services",
        )
        assert s.ticker_id is None
        assert s.timeframe is None
        assert s.industry_name == "IT - Services"

    def test_frozen(self):
        s = self._make()
        with pytest.raises(FrozenInstanceError):
            s.med_ema = 0.0  # type: ignore[misc]

    def test_slots(self):
        """Same defensive pattern as Phase 4A's domain tests —
        frozen+slots raises confusing-looking errors on assignment
        to undefined fields. Either AttributeError or TypeError
        is fine."""
        s = self._make()
        with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
            s.new_field = 1  # type: ignore[attr-defined]

    def test_optional_fields_can_be_None(self):
        """Distance / bars-since / pct-change fields are nullable
        for early bars where they're undefined."""
        s = self._make(
            dist_to_med_in_hl_units=None,
            dist_to_long_in_hl_units=None,
            dist_to_longest_in_hl_units=None,
            mean_dist_to_med_in_hl_units=None,
            mean_dist_to_long_in_hl_units=None,
            mean_dist_to_longest_in_hl_units=None,
            bars_since_med_turn=None,
            bars_since_long_turn=None,
            bars_since_longest_turn=None,
            pct_change_since_med_turn=None,
            pct_change_since_long_turn=None,
            pct_change_since_longest_turn=None,
        )
        assert s.bars_since_med_turn is None
        assert s.dist_to_med_in_hl_units is None

    def test_pattern_label_value_is_string(self):
        """The DB column is VARCHAR(32). The dataclass accepts any
        string; runtime validation happens in the repo layer."""
        s = self._make(med_pattern_label="2 SUCCESSIVE HIGHER VALLEYS")
        assert s.med_pattern_label == "2 SUCCESSIVE HIGHER VALLEYS"

    def test_all_regime_values_accepted(self):
        for v in (-1, 0, 1):
            s = self._make(display_regime=v)
            assert s.display_regime == v


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
            "ticker_id": None,
            "timeframe": None,
            "industry_name": "IT - Services",
            "trade_date": date(2026, 5, 22),
            "event_kind": "BUY",
            "regime_before": 0,
            "regime_after": 1,
            "close_at_event": 844.27,
            "computed_at": datetime(2026, 5, 22, 16, 0, tzinfo=timezone.utc),
        }
        defaults.update(overrides)
        return SignalEvent(**defaults)

    def test_can_construct_industry_keyed(self):
        e = self._make()
        assert e.industry_name == "IT - Services"
        assert e.ticker_id is None
        assert e.event_kind == "BUY"

    def test_can_construct_ticker_keyed(self):
        e = self._make(
            ticker_id=1, timeframe="1w", industry_name=None,
        )
        assert e.ticker_id == 1
        assert e.timeframe == "1w"
        assert e.industry_name is None

    def test_frozen(self):
        e = self._make()
        with pytest.raises(FrozenInstanceError):
            e.event_kind = "SELL"  # type: ignore[misc]

    def test_all_event_kinds_accepted(self):
        for k in SIGNAL_EVENT_KINDS:
            e = self._make(event_kind=k)
            assert e.event_kind == k
