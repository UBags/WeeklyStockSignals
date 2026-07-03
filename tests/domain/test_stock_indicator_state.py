"""Tests for the StockIndicatorState dataclass.

Pure unit tests — no DB, no engine. The dataclass is frozen + slotted
so the tests verify constructor wiring and immutability.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

import pytest

from trip_ema.domain.stock_indicator_state import (
    REGIME_VALUES,
    StockIndicatorState,
)


def _make_state(**overrides) -> StockIndicatorState:
    defaults = {
        "ticker_id": 1,
        "timeframe": "1W",
        "date": date(2026, 6, 22),
        "bar_timestamp": datetime(2026, 6, 22, tzinfo=timezone.utc),
        "med_ema": 100.0,
        "long_ema": 100.0,
        "longest_ema": 100.0,
        "atr": 5.0,
        "upper_inner_band": 105.0,
        "lower_inner_band": 95.0,
        "upper_mid_band": 110.0,
        "lower_mid_band": 90.0,
        "upper_outer_band": 120.0,
        "lower_outer_band": 80.0,
        "display_regime": 1,
        "computed_at": datetime(2026, 6, 22, 16, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return StockIndicatorState(**defaults)


def test_regime_values_constant():
    """REGIME_VALUES is the allowed set for display_regime, mirrored
    by the DB CHECK constraint in migration 0020."""
    assert REGIME_VALUES == (-1, 0, 1)


def test_construct_with_all_fields():
    s = _make_state()
    assert s.ticker_id == 1
    assert s.timeframe == "1W"
    assert s.med_ema == 100.0
    assert s.display_regime == 1


def test_immutability_via_frozen():
    s = _make_state()
    with pytest.raises(FrozenInstanceError):
        s.med_ema = 200.0  # type: ignore[misc]


def test_slots_prevent_arbitrary_attributes():
    s = _make_state()
    # Python 3.12 raises TypeError when setting an undeclared
    # attribute on a frozen+slots dataclass — the dataclass's
    # __setattr__ guard collides with slot lookup. Accept any
    # exception that means "can't set that".
    with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
        s.arbitrary_attr = 42  # type: ignore[attr-defined]


def test_dataclass_does_not_carry_enrichment_fields():
    """The point of the V1 split — the StockIndicatorState dataclass
    must NOT have enrichment fields (those live on
    StockIndicatorEnrichment, added in Step 5).

    This test pins the lean shape so a refactor can't quietly re-merge
    enrichments back into the core dataclass.
    """
    s = _make_state()
    forbidden_enrichment_fields = [
        # band-pierce flags
        "pierces_upper_mid", "pierces_lower_mid",
        # distance metrics
        "dist_to_med_in_hl_units", "dist_to_long_in_hl_units",
        "dist_to_longest_in_hl_units",
        "mean_dist_to_med_in_hl_units", "mean_dist_to_long_in_hl_units",
        "mean_dist_to_longest_in_hl_units",
        # bars / pct change since each EMA's last turn
        "bars_since_med_turn", "bars_since_long_turn",
        "bars_since_longest_turn",
        "pct_change_since_med_turn", "pct_change_since_long_turn",
        "pct_change_since_longest_turn",
        # pattern labels
        "med_pattern_label", "long_pattern_label",
        "longest_pattern_label",
        # turn flags
        "med_turn_buy", "med_turn_sell",
        "long_turn_buy", "long_turn_sell",
        "longest_turn_buy", "longest_turn_sell",
    ]
    for field_name in forbidden_enrichment_fields:
        assert not hasattr(s, field_name), (
            f"StockIndicatorState should NOT carry the enrichment "
            f"field {field_name!r}. Enrichment lives on "
            f"StockIndicatorEnrichment (Step 5 of clean-pipeline V1)."
        )
