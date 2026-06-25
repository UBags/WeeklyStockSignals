"""Unit tests for signals_helpers.signal_industry_mcap_sort_key.

Qt-free. Covers the same ordering contract that the Tickers tab proxy
and the Signals tab dropdown rely on.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trip_ema.gui.signals_helpers import (
    SIGNAL_GROUP_RANK,
    UNSIGNALLED_RANK,
    signal_industry_mcap_sort_key,
)


@dataclass
class _Row:
    symbol: str
    industry: str | None = None
    market_cap: Decimal | None = None


def _sorted_symbols(items: list[tuple[_Row, str | None]]) -> list[str]:
    items_sorted = sorted(
        items, key=lambda pair: signal_industry_mcap_sort_key(*pair),
    )
    return [r.symbol for r, _ in items_sorted]


# ---------------- Signal-group ordering ---------------- #


def test_buy_before_sell_before_exit_before_unsignalled():
    rows = [
        (_Row("UNS"), None),
        (_Row("EXB"), "EXIT_FROM_BUY"),
        (_Row("SEL"), "SELL"),
        (_Row("BUY"), "BUY"),
        (_Row("EXS"), "EXIT_FROM_SELL"),
    ]
    out = _sorted_symbols(rows)
    assert out[0] == "BUY"
    assert out[1] == "SEL"
    assert set(out[2:4]) == {"EXB", "EXS"}
    assert out[-1] == "UNS"


def test_unknown_signal_treated_as_unsignalled():
    rows = [
        (_Row("ZULU"), "FUTURE_KIND"),
        (_Row("ALPHA"), "BUY"),
    ]
    assert _sorted_symbols(rows) == ["ALPHA", "ZULU"]


# ---------------- Industry ordering within a signal group ---------------- #


def test_industries_sort_alphabetically_within_same_signal_group():
    rows = [
        (_Row("BBB", industry="Specialty Chemicals",
              market_cap=Decimal("100")), "BUY"),
        (_Row("AAA", industry="Banks - Private Sector",
              market_cap=Decimal("100")), "BUY"),
    ]
    # "Banks - Private Sector" < "Specialty Chemicals" alphabetically.
    assert _sorted_symbols(rows) == ["AAA", "BBB"]


def test_null_industry_falls_to_bottom_of_its_signal_group():
    rows = [
        (_Row("HASIND", industry="Banks", market_cap=Decimal("100")), "BUY"),
        (_Row("NULIND", industry=None, market_cap=Decimal("999999")), "BUY"),
    ]
    # NULL industry → after every tagged industry in the same group,
    # regardless of mcap.
    assert _sorted_symbols(rows) == ["HASIND", "NULIND"]


# ---------------- Market cap ordering within an industry ---------------- #


def test_within_same_industry_mcap_desc_wins():
    rows = [
        (_Row("SMALL", industry="Banks",
              market_cap=Decimal("100")), "BUY"),
        (_Row("LARGE", industry="Banks",
              market_cap=Decimal("10000")), "BUY"),
        (_Row("MID", industry="Banks",
              market_cap=Decimal("5000")), "BUY"),
    ]
    assert _sorted_symbols(rows) == ["LARGE", "MID", "SMALL"]


def test_null_mcap_falls_to_bottom_of_its_industry():
    rows = [
        (_Row("HASMC", industry="Banks", market_cap=Decimal("100")), "BUY"),
        (_Row("NOMC", industry="Banks", market_cap=None), "BUY"),
    ]
    assert _sorted_symbols(rows) == ["HASMC", "NOMC"]


def test_symbol_breaks_ties_when_industry_and_mcap_match():
    rows = [
        (_Row("BETA", industry="Banks", market_cap=Decimal("100")), "BUY"),
        (_Row("alpha", industry="Banks", market_cap=Decimal("100")), "BUY"),
    ]
    assert _sorted_symbols(rows) == ["alpha", "BETA"]


# ---------------- Cross-group / cross-industry interactions ---------------- #


def test_industry_sort_is_scoped_within_signal_group():
    """A BUY in industry Z still beats a SELL in industry A — signal
    group is the outermost key."""
    rows = [
        (_Row("AAA-SELL", industry="Aerospace",
              market_cap=Decimal("99999")), "SELL"),
        (_Row("ZZZ-BUY", industry="Zinc Mining",
              market_cap=Decimal("1")), "BUY"),
    ]
    assert _sorted_symbols(rows) == ["ZZZ-BUY", "AAA-SELL"]


# ---------------- Constants surfaced for callers ---------------- #


def test_signal_group_rank_includes_all_four_known_types():
    assert set(SIGNAL_GROUP_RANK.keys()) == {
        "BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL",
    }


def test_exit_buy_and_exit_sell_share_a_rank():
    assert (
        SIGNAL_GROUP_RANK["EXIT_FROM_BUY"]
        == SIGNAL_GROUP_RANK["EXIT_FROM_SELL"]
    )


def test_unsignalled_rank_is_strictly_greater_than_signalled_ranks():
    assert max(SIGNAL_GROUP_RANK.values()) < UNSIGNALLED_RANK