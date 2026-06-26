"""Tests for the IndustryIndicatorEnrichment dataclass.

Mirror of test_stock_indicator_enrichment / test_industry_indicator_state.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, UTC

import pytest

from trip_ema.domain.industry_indicator_enrichment import (
    PATTERN_LABELS,
    IndustryIndicatorEnrichment,
)


def _make(**overrides) -> IndustryIndicatorEnrichment:
    defaults = dict(
        industry_name="IT - Services",
        timeframe="Weekly",
        bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
        pierces_upper_mid=False,
        pierces_lower_mid=False,
        dist_to_med_in_hl_units=0.5,
        dist_to_long_in_hl_units=0.4,
        dist_to_longest_in_hl_units=0.3,
        mean_dist_to_med_in_hl_units=0.55,
        mean_dist_to_long_in_hl_units=0.45,
        mean_dist_to_longest_in_hl_units=0.35,
        bars_since_med_turn=5,
        bars_since_long_turn=8,
        bars_since_longest_turn=12,
        pct_change_since_med_turn=0.05,
        pct_change_since_long_turn=0.08,
        pct_change_since_longest_turn=0.12,
        med_pattern_label="NA",
        long_pattern_label="NA",
        longest_pattern_label="NA",
        med_turn_buy=False,
        med_turn_sell=False,
        long_turn_buy=False,
        long_turn_sell=False,
        longest_turn_buy=False,
        longest_turn_sell=False,
        computed_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return IndustryIndicatorEnrichment(**defaults)


class TestPatternLabelsConstant:

    def test_pattern_labels_tuple(self):
        assert isinstance(PATTERN_LABELS, tuple)
        assert "NA" in PATTERN_LABELS
        # Lock in the legacy 5-label set
        assert len(PATTERN_LABELS) == 5


class TestIndustryIndicatorEnrichment:

    def test_basic_construction(self):
        e = _make()
        assert e.industry_name == "IT - Services"
        assert e.timeframe == "Weekly"
        assert e.med_pattern_label == "NA"
        assert e.bars_since_med_turn == 5

    def test_frozen(self):
        e = _make()
        with pytest.raises(FrozenInstanceError):
            e.pierces_upper_mid = True  # type: ignore[misc]

    def test_slots_no_undeclared_attrs(self):
        e = _make()
        with pytest.raises((AttributeError, TypeError)):
            e.brand_new = 42  # type: ignore[attr-defined]

    def test_field_count(self):
        """Lock in 27 fields: 3 PK + 23 enrichment + 1 audit."""
        fs = fields(IndustryIndicatorEnrichment)
        assert len(fs) == 27

    def test_expected_field_names(self):
        fs = {f.name for f in fields(IndustryIndicatorEnrichment)}
        expected = {
            "industry_name", "timeframe", "bar_timestamp",
            "pierces_upper_mid", "pierces_lower_mid",
            "dist_to_med_in_hl_units", "dist_to_long_in_hl_units",
            "dist_to_longest_in_hl_units",
            "mean_dist_to_med_in_hl_units",
            "mean_dist_to_long_in_hl_units",
            "mean_dist_to_longest_in_hl_units",
            "bars_since_med_turn", "bars_since_long_turn",
            "bars_since_longest_turn",
            "pct_change_since_med_turn",
            "pct_change_since_long_turn",
            "pct_change_since_longest_turn",
            "med_pattern_label", "long_pattern_label",
            "longest_pattern_label",
            "med_turn_buy", "med_turn_sell",
            "long_turn_buy", "long_turn_sell",
            "longest_turn_buy", "longest_turn_sell",
            "computed_at",
        }
        assert fs == expected

    def test_nullable_distance_fields(self):
        """Nullable fields should accept None."""
        e = _make(
            dist_to_med_in_hl_units=None,
            dist_to_long_in_hl_units=None,
            bars_since_med_turn=None,
            pct_change_since_med_turn=None,
        )
        assert e.dist_to_med_in_hl_units is None
        assert e.bars_since_med_turn is None
