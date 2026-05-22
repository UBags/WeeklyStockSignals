"""Unit tests for Phase 4B-1 domain types.

Locks in the *_SOURCES tuples and the dataclass contracts (frozen
+ slots). Same shape as test_market_cap_domain.py.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone

import pytest

from trip_ema.fundamentals import (
    CONSTITUENT_REMOVED_REASONS,
    INDEX_METHODOLOGIES,
    INDEX_STATUSES,
    REBALANCE_SCHEDULES,
    IndustryIndex,
    IndustryIndexConstituent,
    IndustryIndexValue,
)


class TestAllowedValuesTuples:

    def test_methodologies(self):
        assert INDEX_METHODOLOGIES == (
            "MODIFIED_LASPEYRES",
            "EQUAL_WEIGHTED",
            "FREE_FLOAT_WEIGHTED",
        )

    def test_schedules(self):
        assert REBALANCE_SCHEDULES == (
            "SEMIANNUAL_APR_OCT",
            "QUARTERLY",
            "ANNUAL_APR",
            "MANUAL",
        )

    def test_statuses(self):
        assert INDEX_STATUSES == ("ACTIVE", "RETIRED")

    def test_removed_reasons(self):
        assert CONSTITUENT_REMOVED_REASONS == (
            "REBALANCE_EXIT", "DELISTED", "OPERATOR_REMOVED",
        )

    def test_all_are_tuples(self):
        for v in (INDEX_METHODOLOGIES, REBALANCE_SCHEDULES,
                  INDEX_STATUSES, CONSTITUENT_REMOVED_REASONS):
            assert isinstance(v, tuple)


class TestIndustryIndexDataclass:

    def _make(self, **overrides):
        defaults = {
            "industry_name": "Banks",
            "sector_name": "Financials",
            "methodology": "MODIFIED_LASPEYRES",
            "base_value": 1000.0,
            "base_date": date(2025, 10, 1),
            "rebalance_schedule": "SEMIANNUAL_APR_OCT",
            "min_constituents": 10,
            "status": "ACTIVE",
            "notes": None,
        }
        defaults.update(overrides)
        return IndustryIndex(**defaults)

    def test_can_construct(self):
        m = self._make()
        assert m.industry_name == "Banks"
        assert m.base_value == 1000.0

    def test_frozen(self):
        m = self._make()
        with pytest.raises(FrozenInstanceError):
            m.industry_name = "X"  # type: ignore[misc]

    def test_slots(self):
        m = self._make()
        with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
            m.new_field = 1  # type: ignore[attr-defined]


class TestIndustryIndexConstituentDataclass:

    def _make(self, **overrides):
        defaults = {
            "industry_name": "Banks",
            "ticker_id": 1,
            "rebalance_date": date(2025, 10, 1),
            "effective_from": date(2025, 10, 1),
            "effective_to": None,
            "weight_at_rebalance": 0.15,
            "market_cap_at_rebalance": 1.5e12,
            "close_at_rebalance": 1500.0,
            "removed_reason": None,
        }
        defaults.update(overrides)
        return IndustryIndexConstituent(**defaults)

    def test_can_construct_active(self):
        c = self._make()
        assert c.effective_to is None
        assert c.removed_reason is None

    def test_can_construct_closed(self):
        c = self._make(
            effective_to=date(2026, 3, 31),
            removed_reason="REBALANCE_EXIT",
        )
        assert c.effective_to == date(2026, 3, 31)
        assert c.removed_reason == "REBALANCE_EXIT"

    def test_frozen(self):
        c = self._make()
        with pytest.raises(FrozenInstanceError):
            c.weight_at_rebalance = 0.0  # type: ignore[misc]


class TestIndustryIndexValueDataclass:

    def _make(self, **overrides):
        defaults = {
            "industry_name": "Banks",
            "trade_date": date(2025, 10, 15),
            "value": 1025.0,
            "constituents_used": 10,
            "constituents_missing": 0,
            "renormalized": False,
            "effective_rebalance_date": date(2025, 10, 1),
            "computed_at": datetime(2025, 10, 15, 16, 0, tzinfo=timezone.utc),
        }
        defaults.update(overrides)
        return IndustryIndexValue(**defaults)

    def test_can_construct_clean_day(self):
        v = self._make()
        assert v.constituents_used == 10
        assert v.constituents_missing == 0
        assert v.renormalized is False

    def test_can_construct_renormalized_day(self):
        """Day with a delisted constituent — renormalized=True,
        constituents_missing>0, constituents_used<10."""
        v = self._make(
            constituents_used=9,
            constituents_missing=1,
            renormalized=True,
        )
        assert v.renormalized is True

    def test_frozen(self):
        v = self._make()
        with pytest.raises(FrozenInstanceError):
            v.value = 0.0  # type: ignore[misc]
