"""Unit tests for signals_helpers.industry_rank_alpha_sort_key.

Step 12b: pins the new industry-grouped sort spec used by both the
Tickers tab proxy and the Signals dropdown.

Ordering, in priority:
  1. industry ASC, NULL last
  2. Within industry: signaled (any of BUY/SELL/EXIT_*) before
     unsignaled
  3. Within signaled: rank_score DESC (missing = 0.0)
  4. Within unsignaled: symbol ASC
  5. symbol ASC as a final tiebreak

Lives in tests/gui_pure/ since the helper is Qt-free.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from trip_ema.gui.signals_helpers import (
    industry_rank_alpha_sort_key,
)


@dataclass(frozen=True)
class _Row:
    """Stub matching the duck-typed row shape the sort key reads."""
    symbol: str
    industry: str | None = None
    market_cap: Decimal | None = None  # ignored by this sort key


def _sort(rows_with_meta):
    """rows_with_meta = list[tuple[_Row, signal_type_or_None,
    rank_score_or_None]]. Returns the rows in sort-key order."""
    return [
        r for (r, _sig, _rank) in sorted(
            rows_with_meta,
            key=lambda t: industry_rank_alpha_sort_key(*t),
        )
    ]


# ============================================================
# Industry grouping is primary
# ============================================================

def test_industry_alphabetical():
    items = [
        (_Row("A", "Energy"), "BUY", 50.0),
        (_Row("B", "Banking"), "BUY", 50.0),
        (_Row("C", "Auto"), "BUY", 50.0),
    ]
    result = _sort(items)
    assert [r.industry for r in result] == [
        "Auto", "Banking", "Energy",
    ]


def test_industry_null_sorts_last():
    items = [
        (_Row("A", None), "BUY", 50.0),
        (_Row("B", "Banking"), "BUY", 50.0),
    ]
    result = _sort(items)
    assert [r.industry for r in result] == ["Banking", None]


def test_industry_case_insensitive():
    items = [
        (_Row("A", "Energy"), "BUY", 50.0),
        (_Row("B", "banking"), "BUY", 50.0),
        (_Row("C", "AUTO"), "BUY", 50.0),
    ]
    result = _sort(items)
    assert [r.industry for r in result] == [
        "AUTO", "banking", "Energy",
    ]


# ============================================================
# Within an industry: signaled before unsignaled
# ============================================================

def test_signaled_before_unsignaled_within_industry():
    items = [
        (_Row("UNSIG", "Banking"), None, None),
        (_Row("SIG", "Banking"), "BUY", 50.0),
    ]
    result = _sort(items)
    assert [r.symbol for r in result] == ["SIG", "UNSIG"]


def test_all_signal_types_count_as_signaled():
    """BUY, SELL, EXIT_FROM_BUY, EXIT_FROM_SELL all sort before
    unsignaled. Within the signaled subgroup, rank_score decides
    order (no internal BUY-before-SELL preference)."""
    items = [
        (_Row("BUY1", "X"), "BUY", 50.0),
        (_Row("SELL1", "X"), "SELL", 80.0),
        (_Row("EXIT1", "X"), "EXIT_FROM_BUY", 70.0),
        (_Row("UNS1", "X"), None, None),
    ]
    result = _sort(items)
    # By rank DESC: SELL(80) > EXIT(70) > BUY(50), then unsignaled
    assert [r.symbol for r in result] == [
        "SELL1", "EXIT1", "BUY1", "UNS1",
    ]


# ============================================================
# Within signaled: rank_score DESC
# ============================================================

def test_signaled_rank_score_desc():
    items = [
        (_Row("LOW", "Banking"), "BUY", 10.0),
        (_Row("HIGH", "Banking"), "BUY", 90.0),
        (_Row("MID", "Banking"), "BUY", 50.0),
    ]
    result = _sort(items)
    assert [r.symbol for r in result] == ["HIGH", "MID", "LOW"]


def test_missing_rank_score_treated_as_zero():
    """A signaled row with no rank_score sorts below signaled rows
    that have positive rank scores, but above unsignaled rows."""
    items = [
        (_Row("NORANK", "Banking"), "BUY", None),
        (_Row("HIGH", "Banking"), "BUY", 90.0),
        (_Row("UNS", "Banking"), None, None),
    ]
    result = _sort(items)
    assert [r.symbol for r in result] == ["HIGH", "NORANK", "UNS"]


def test_signaled_rank_tie_breaks_by_symbol():
    items = [
        (_Row("ZETA", "X"), "BUY", 50.0),
        (_Row("ALPHA", "X"), "BUY", 50.0),
        (_Row("MIKE", "X"), "BUY", 50.0),
    ]
    result = _sort(items)
    assert [r.symbol for r in result] == ["ALPHA", "MIKE", "ZETA"]


# ============================================================
# Within unsignaled: alphabetical
# ============================================================

def test_unsignaled_symbol_asc():
    items = [
        (_Row("ZULU", "Banking"), None, None),
        (_Row("ALPHA", "Banking"), None, None),
        (_Row("MIKE", "Banking"), None, None),
    ]
    result = _sort(items)
    assert [r.symbol for r in result] == ["ALPHA", "MIKE", "ZULU"]


def test_unsignaled_ignores_rank_score():
    """Even if rank_score is supplied for unsignaled rows
    (defensive — caller might pass it), they still sort
    alphabetically by symbol."""
    items = [
        (_Row("ZULU", "X"), None, 90.0),
        (_Row("ALPHA", "X"), None, 10.0),
    ]
    result = _sort(items)
    assert [r.symbol for r in result] == ["ALPHA", "ZULU"]


# ============================================================
# Cross-industry composite
# ============================================================

def test_full_layout_two_industries():
    """End-to-end: two industries × (signaled high, signaled low,
    unsignaled). Verify the full nested ordering."""
    items = [
        (_Row("AAA", "Auto"), "BUY", 90.0),
        (_Row("AAB", "Auto"), "SELL", 60.0),
        (_Row("AAC", "Auto"), None, None),
        (_Row("AAD", "Auto"), None, None),
        (_Row("BBA", "Banking"), "BUY", 80.0),
        (_Row("BBB", "Banking"), "EXIT_FROM_BUY", 50.0),
        (_Row("BBC", "Banking"), None, None),
    ]
    result = _sort(items)
    assert [r.symbol for r in result] == [
        # Auto industry first
        "AAA",  # signaled, rank 90
        "AAB",  # signaled, rank 60
        "AAC",  # unsignaled, alpha
        "AAD",  # unsignaled, alpha
        # Banking industry second
        "BBA",  # signaled, rank 80
        "BBB",  # signaled, rank 50
        "BBC",  # unsignaled
    ]


def test_missing_industry_handled_gracefully():
    """Rows with industry=None sink to the end but the within-group
    ordering still applies."""
    items = [
        (_Row("A", None), "BUY", 90.0),
        (_Row("B", None), None, None),
        (_Row("C", "Auto"), "BUY", 50.0),
    ]
    result = _sort(items)
    assert [r.symbol for r in result] == ["C", "A", "B"]
