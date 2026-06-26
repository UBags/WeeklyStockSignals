"""Tests for the IndustryIndicatorState dataclass.

Verifies the dataclass shape, immutability, slots discipline, and the
REGIME_VALUES constant. Mirror of test_stock_indicator_state.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, UTC

import pytest

from trip_ema.domain.industry_indicator_state import (
    REGIME_VALUES,
    IndustryIndicatorState,
)


def _make(**overrides) -> IndustryIndicatorState:
    defaults = dict(
        industry_name="IT - Services",
        timeframe="Weekly",
        bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
        med_ema=100.0,
        long_ema=99.0,
        longest_ema=98.0,
        atr=5.0,
        upper_inner_band=105.0,
        lower_inner_band=95.0,
        upper_mid_band=110.0,
        lower_mid_band=90.0,
        upper_outer_band=115.0,
        lower_outer_band=85.0,
        display_regime=1,
        computed_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return IndustryIndicatorState(**defaults)


class TestRegimeValuesConstant:

    def test_regime_values_tuple(self):
        assert REGIME_VALUES == (-1, 0, 1)
        assert isinstance(REGIME_VALUES, tuple)


class TestIndustryIndicatorState:

    def test_basic_construction(self):
        s = _make()
        assert s.industry_name == "IT - Services"
        assert s.timeframe == "Weekly"
        assert s.display_regime == 1
        assert s.med_ema == 100.0

    def test_frozen(self):
        """The dataclass should be immutable."""
        s = _make()
        # Python 3.12 frozen+slots raises TypeError, not
        # FrozenInstanceError, when assigning to an undeclared
        # attr; for a DECLARED attr it raises FrozenInstanceError.
        with pytest.raises(FrozenInstanceError):
            s.atr = 99.0  # type: ignore[misc]

    def test_slots_no_undeclared_attrs(self):
        """slots=True forbids adding new attributes."""
        s = _make()
        with pytest.raises((AttributeError, TypeError)):
            s.brand_new_attribute = 42  # type: ignore[attr-defined]

    def test_field_count(self):
        """Lock in the 15-field shape (3 PK + 11 indicator + 1 audit)."""
        fs = fields(IndustryIndicatorState)
        assert len(fs) == 15

    def test_expected_field_names(self):
        fs = {f.name for f in fields(IndustryIndicatorState)}
        expected = {
            "industry_name", "timeframe", "bar_timestamp",
            "med_ema", "long_ema", "longest_ema",
            "atr",
            "upper_inner_band", "lower_inner_band",
            "upper_mid_band", "lower_mid_band",
            "upper_outer_band", "lower_outer_band",
            "display_regime",
            "computed_at",
        }
        assert fs == expected

    @pytest.mark.parametrize("regime_value", [-1, 0, 1])
    def test_accepts_all_regime_values(self, regime_value):
        s = _make(display_regime=regime_value)
        assert s.display_regime == regime_value
