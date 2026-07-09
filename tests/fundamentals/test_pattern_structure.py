"""Unit tests for the full HH/HL/LH/LL structure + composite functions
added to pattern_rules (spec v0.3 §6)."""
from trip_ema.fundamentals.pattern_rules import (
    COMPOSITE_STRUCTURES,
    REL_HH,
    REL_HL,
    REL_LH,
    REL_LL,
    REL_NA,
    STRUCT_BROADENING,
    STRUCT_CONTRACTING,
    STRUCT_DOWNTREND,
    STRUCT_NA,
    STRUCT_UPTREND,
    STRUCTURE_RELATIONS,
    composite_structure,
    peak_relation,
    structure_labels,
    trough_relation,
)


class TestPeakRelation:
    def test_higher_high(self):
        assert peak_relation(110.0, 100.0) == REL_HH

    def test_lower_high(self):
        assert peak_relation(90.0, 100.0) == REL_LH

    def test_equal_is_na(self):
        assert peak_relation(100.0, 100.0) == REL_NA

    def test_missing_is_na(self):
        assert peak_relation(None, 100.0) == REL_NA
        assert peak_relation(100.0, None) == REL_NA
        assert peak_relation(float("nan"), 100.0) == REL_NA


class TestTroughRelation:
    def test_higher_low(self):
        assert trough_relation(60.0, 50.0) == REL_HL

    def test_lower_low(self):
        assert trough_relation(40.0, 50.0) == REL_LL

    def test_equal_is_na(self):
        assert trough_relation(50.0, 50.0) == REL_NA

    def test_missing_is_na(self):
        assert trough_relation(None, 50.0) == REL_NA
        assert trough_relation(50.0, float("nan")) == REL_NA


class TestCompositeStructure:
    def test_uptrend(self):
        assert composite_structure(REL_HH, REL_HL) == STRUCT_UPTREND

    def test_downtrend(self):
        assert composite_structure(REL_LH, REL_LL) == STRUCT_DOWNTREND

    def test_broadening(self):
        assert composite_structure(REL_HH, REL_LL) == STRUCT_BROADENING

    def test_contracting(self):
        assert composite_structure(REL_LH, REL_HL) == STRUCT_CONTRACTING

    def test_na_when_either_missing(self):
        assert composite_structure(REL_NA, REL_HL) == STRUCT_NA
        assert composite_structure(REL_HH, REL_NA) == STRUCT_NA
        assert composite_structure(REL_NA, REL_NA) == STRUCT_NA


class TestStructureLabels:
    def test_uptrend_end_to_end(self):
        p, t, c = structure_labels(
            last_peak_value=120.0, prev_peak_value=110.0,
            last_trough_value=95.0, prev_trough_value=85.0,
        )
        assert (p, t, c) == (REL_HH, REL_HL, STRUCT_UPTREND)

    def test_downtrend_end_to_end(self):
        p, t, c = structure_labels(
            last_peak_value=100.0, prev_peak_value=110.0,
            last_trough_value=80.0, prev_trough_value=90.0,
        )
        assert (p, t, c) == (REL_LH, REL_LL, STRUCT_DOWNTREND)

    def test_contracting_end_to_end(self):
        # lower highs + higher lows = coil
        p, t, c = structure_labels(
            last_peak_value=105.0, prev_peak_value=110.0,
            last_trough_value=95.0, prev_trough_value=90.0,
        )
        assert (p, t, c) == (REL_LH, REL_HL, STRUCT_CONTRACTING)

    def test_broadening_end_to_end(self):
        p, t, c = structure_labels(
            last_peak_value=120.0, prev_peak_value=110.0,
            last_trough_value=80.0, prev_trough_value=90.0,
        )
        assert (p, t, c) == (REL_HH, REL_LL, STRUCT_BROADENING)

    def test_na_propagates(self):
        p, t, c = structure_labels(
            last_peak_value=None, prev_peak_value=110.0,
            last_trough_value=95.0, prev_trough_value=85.0,
        )
        assert p == REL_NA
        assert t == REL_HL
        assert c == STRUCT_NA


def test_canonical_sets_and_exhaustive():
    assert set(STRUCTURE_RELATIONS) == {REL_NA, REL_HH, REL_LH, REL_HL, REL_LL}
    assert set(COMPOSITE_STRUCTURES) == {
        STRUCT_NA, STRUCT_UPTREND, STRUCT_DOWNTREND,
        STRUCT_BROADENING, STRUCT_CONTRACTING,
    }
    # every relation pair maps into the canonical composite set
    for pr in STRUCTURE_RELATIONS:
        for tr in STRUCTURE_RELATIONS:
            assert composite_structure(pr, tr) in COMPOSITE_STRUCTURES
