"""Tests for the StockIndicatorEnrichment dataclass.

Pure unit tests — no DB, no engine. Mirrors the StockIndicatorState
tests, including the "no core fields" pin (the dataclass must NOT
carry EMAs / ATR / bands / regime — those live on
StockIndicatorState).
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from trip_ema.domain.stock_indicator_enrichment import (
    PATTERN_LABELS,
    StockIndicatorEnrichment,
)


def _make_enrichment(**overrides) -> StockIndicatorEnrichment:
    defaults = {
        "ticker_id": 1,
        "timeframe": "1W",
        "bar_timestamp": datetime(2026, 6, 22, tzinfo=timezone.utc),
        "pierces_upper_mid": False,
        "pierces_lower_mid": False,
        "dist_to_med_in_hl_units": 0.5,
        "dist_to_long_in_hl_units": 0.7,
        "dist_to_longest_in_hl_units": 1.0,
        "mean_dist_to_med_in_hl_units": 0.4,
        "mean_dist_to_long_in_hl_units": 0.6,
        "mean_dist_to_longest_in_hl_units": 0.9,
        "bars_since_med_turn": 3,
        "bars_since_long_turn": 5,
        "bars_since_longest_turn": 10,
        "pct_change_since_med_turn": 0.02,
        "pct_change_since_long_turn": 0.05,
        "pct_change_since_longest_turn": 0.10,
        "med_pattern_label": "NA",
        "long_pattern_label": "NA",
        "longest_pattern_label": "NA",
        "med_turn_buy": False,
        "med_turn_sell": False,
        "long_turn_buy": False,
        "long_turn_sell": False,
        "longest_turn_buy": False,
        "longest_turn_sell": False,
        "computed_at": datetime(2026, 6, 22, 16, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return StockIndicatorEnrichment(**defaults)


def test_pattern_labels_constant_complete():
    """PATTERN_LABELS must include 'NA' (the default) plus the four
    pattern detections from Pine — mirrored from the operator's spec."""
    assert "NA" in PATTERN_LABELS
    assert "1 SUCCESSIVE HIGHER VALLEY" in PATTERN_LABELS
    assert "2 SUCCESSIVE HIGHER VALLEYS" in PATTERN_LABELS
    assert "1 SUCCESSIVE LOWER TOP" in PATTERN_LABELS
    assert "2 SUCCESSIVE LOWER TOPS" in PATTERN_LABELS


def test_construct_with_all_fields():
    e = _make_enrichment()
    assert e.ticker_id == 1
    assert e.timeframe == "1W"
    assert e.pierces_upper_mid is False
    assert e.med_pattern_label == "NA"


def test_construct_with_nullable_fields_as_none():
    """The 12 nullable fields can legitimately be None during warmup."""
    e = _make_enrichment(
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
    assert e.dist_to_med_in_hl_units is None
    assert e.bars_since_med_turn is None


def test_immutability_via_frozen():
    e = _make_enrichment()
    with pytest.raises(FrozenInstanceError):
        e.pierces_upper_mid = True  # type: ignore[misc]


def test_slots_prevent_arbitrary_attributes():
    e = _make_enrichment()
    # Python 3.12 raises TypeError for undeclared attrs on
    # frozen+slots dataclasses.
    with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
        e.arbitrary_attr = 42  # type: ignore[attr-defined]


def test_dataclass_does_not_carry_core_state_fields():
    """The point of the V1 split — StockIndicatorEnrichment must NOT
    have core indicator fields (EMAs, ATR, bands, regime). Those live
    on StockIndicatorState. This pins the lean shape."""
    e = _make_enrichment()
    forbidden_core_fields = [
        "med_ema", "long_ema", "longest_ema",
        "atr",
        "upper_inner_band", "lower_inner_band",
        "upper_mid_band", "lower_mid_band",
        "upper_outer_band", "lower_outer_band",
        "display_regime",
    ]
    for field_name in forbidden_core_fields:
        assert not hasattr(e, field_name), (
            f"StockIndicatorEnrichment should NOT carry the core "
            f"field {field_name!r}. Core state lives on "
            f"StockIndicatorState (Step 4 of clean-pipeline V1)."
        )


def test_all_enrichment_fields_present():
    """Pin the full enrichment field set so a refactor can't quietly
    drop or rename one without flagging the test."""
    e = _make_enrichment()
    expected_fields = [
        "pierces_upper_mid", "pierces_lower_mid",
        "dist_to_med_in_hl_units", "dist_to_long_in_hl_units",
        "dist_to_longest_in_hl_units",
        "mean_dist_to_med_in_hl_units", "mean_dist_to_long_in_hl_units",
        "mean_dist_to_longest_in_hl_units",
        "bars_since_med_turn", "bars_since_long_turn",
        "bars_since_longest_turn",
        "pct_change_since_med_turn", "pct_change_since_long_turn",
        "pct_change_since_longest_turn",
        "med_pattern_label", "long_pattern_label",
        "longest_pattern_label",
        "med_turn_buy", "med_turn_sell",
        "long_turn_buy", "long_turn_sell",
        "longest_turn_buy", "longest_turn_sell",
    ]
    for f in expected_fields:
        assert hasattr(e, f), f"missing field {f!r}"
