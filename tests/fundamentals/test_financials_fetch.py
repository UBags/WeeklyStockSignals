"""Tests for the yfinance quarterly financials fetcher.

The fetcher takes a FundamentalsSource (trip_ema/fundamentals/ports.py); these
tests inject a FakeFundamentalsSource. Nothing is patched, and none of these
tests require yfinance to be installed.

Uses real pandas DataFrames because the fetcher does DataFrame-shaped
operations (df.loc, df.columns, df.index).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from .fakes import FakeFundamentalsSource
from trip_ema.fundamentals.financials_fetch import (
    FinancialsFetchError,
    FinancialsFetchPermanent,
    FinancialsFetchRateLimited,
    FinancialsFetchTransient,
    QuarterlyFinancialInfo,
    fetch_quarterly_financials,
)


def _make_df(rows: dict, columns: list[str]) -> pd.DataFrame:
    """Build a DataFrame in the shape yfinance returns: row index =
    line-item names, columns = quarter-end timestamps.

    Example:
        _make_df({
            "Total Revenue": [100, 110, 120, 130],
            "EBITDA":        [15,  17,  18,  20],
        }, ["2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"])
    """
    cols = [pd.Timestamp(c) for c in columns]
    # Construct with rows-as-dict-keys, then transpose so the dict
    # keys become the row index and the supplied columns become
    # the column index.
    return pd.DataFrame(rows, index=cols).T


# Quick sanity check on the helper itself — DataFrame construction
# is error-prone.
def test_helper_builds_correct_shape():
    df = _make_df(
        {"Total Revenue": [100, 110]},
        ["2025-12-31", "2025-09-30"],
    )
    assert list(df.index) == ["Total Revenue"]
    assert len(df.columns) == 2
    assert df.loc["Total Revenue"][df.columns[0]] == 100


# ---------- happy paths ---------- #


class TestHappyPath:

    def test_returns_4_quarters_when_yfinance_has_them(self):
        df = _make_df(
            {
                "Total Revenue": [2500e9, 2400e9, 2300e9, 2200e9],
                "EBITDA":        [350e9,  340e9,  330e9,  320e9],
            },
            ["2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials("RELIANCE", "NSE", source=source)
        assert len(quarters) == 4
        # Newest first per yfinance's column order.
        assert quarters[0].fiscal_quarter == date(2025, 12, 31)
        assert quarters[0].revenue == 2500e9
        assert quarters[0].ebitda == 350e9
        assert quarters[0].currency == "INR"
        assert quarters[3].fiscal_quarter == date(2025, 3, 31)

    def test_respects_max_quarters(self):
        df = _make_df(
            {"Total Revenue": [100, 110, 120, 130]},
            ["2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials(
            "X", "NSE", max_quarters=2,
            source=source,
        )
        assert len(quarters) == 2

    def test_returns_revenue_only_when_ebitda_missing(self):
        """No EBITDA row in the DataFrame → quarters have revenue,
        ebitda=None. This is the common case for Indian mid/small caps."""
        df = _make_df(
            {"Total Revenue": [500, 550]},
            ["2025-12-31", "2025-09-30"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials("X", "NSE", source=source)
        assert len(quarters) == 2
        assert all(q.ebitda is None for q in quarters)
        assert quarters[0].revenue == 500

    def test_returns_ebitda_only_when_revenue_missing(self):
        """Theoretical case: yfinance has EBITDA but not revenue. The
        record is still kept because at least one field is populated."""
        df = _make_df(
            {"EBITDA": [50, 55]},
            ["2025-12-31", "2025-09-30"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials("X", "NSE", source=source)
        assert len(quarters) == 2
        assert all(q.revenue is None for q in quarters)
        assert quarters[0].ebitda == 50


# ---------- yfinance quirks ---------- #


class TestFieldNameVariation:
    """yfinance has used different labels for revenue across companies
    and yfinance versions. The fetcher tries multiple candidates."""

    def test_revenue_under_Revenues_alias(self):
        df = _make_df(
            {"Revenues": [100]},
            ["2025-12-31"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials("X", "NSE", source=source)
        assert quarters[0].revenue == 100

    def test_revenue_under_Revenue_From_Operations(self):
        """Ind AS statutory line name for Indian companies."""
        df = _make_df(
            {"Revenue From Operations": [100]},
            ["2025-12-31"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials("X", "NSE", source=source)
        assert quarters[0].revenue == 100

    def test_ebitda_under_Normalized_EBITDA_alias(self):
        df = _make_df(
            {"Total Revenue": [100], "Normalized EBITDA": [15]},
            ["2025-12-31"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials("X", "NSE", source=source)
        assert quarters[0].ebitda == 15

    def test_Total_Revenue_beats_Revenue_From_Operations(self):
        """If both candidates exist, the first one in
        REVENUE_FIELD_CANDIDATES wins. Test the precedence is correct."""
        df = _make_df(
            {
                "Total Revenue":           [100],
                "Revenue From Operations": [999],  # different number
            },
            ["2025-12-31"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials("X", "NSE", source=source)
        assert quarters[0].revenue == 100  # Total Revenue wins


class TestNaNHandling:
    """pandas returns NaN for missing numeric values. We treat NaN as
    None at the dataclass boundary."""

    def test_nan_in_revenue_becomes_none(self):
        import numpy as np
        df = _make_df(
            {"Total Revenue": [100, float("nan")], "EBITDA": [15, 17]},
            ["2025-12-31", "2025-09-30"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials("X", "NSE", source=source)
        # Both quarters retained because EBITDA is populated for both.
        assert len(quarters) == 2
        assert quarters[0].revenue == 100
        assert quarters[1].revenue is None  # NaN → None
        assert quarters[1].ebitda == 17

    def test_quarter_with_both_nan_is_dropped(self):
        """If both revenue and ebitda are NaN for a quarter, that
        quarter is skipped — no value in recording it."""
        df = _make_df(
            {
                "Total Revenue": [100, float("nan")],
                "EBITDA":        [15,  float("nan")],
            },
            ["2025-12-31", "2025-09-30"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials("X", "NSE", source=source)
        assert len(quarters) == 1
        assert quarters[0].fiscal_quarter == date(2025, 12, 31)


class TestCurrency:

    def test_yfinance_currency_used_when_provided(self):
        df = _make_df({"Total Revenue": [100]}, ["2025-12-31"])
        source = FakeFundamentalsSource(quarterly=(df, "USD"))
        quarters = fetch_quarterly_financials("X", "NYSE", source=source)
        assert quarters[0].currency == "USD"

    def test_INR_default(self):
        df = _make_df({"Total Revenue": [100]}, ["2025-12-31"])
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        quarters = fetch_quarterly_financials("X", "NSE", source=source)
        assert quarters[0].currency == "INR"


# ---------- failure paths ---------- #


class TestFailures:

    def test_empty_dataframe_raises_permanent(self):
        empty = pd.DataFrame()
        source = FakeFundamentalsSource(quarterly=(empty, "INR"))
        with pytest.raises(FinancialsFetchPermanent, match="no quarterly"):
            fetch_quarterly_financials("X", "NSE", source=source)

    def test_none_dataframe_raises_permanent(self):
        source = FakeFundamentalsSource(quarterly=(None, "INR"))
        with pytest.raises(FinancialsFetchPermanent):
            fetch_quarterly_financials("X", "NSE", source=source)

    def test_all_quarters_empty_raises_permanent(self):
        """DataFrame exists but every quarter has NaN for both rev
        and ebitda."""
        df = _make_df(
            {
                "Total Revenue": [float("nan"), float("nan")],
                "EBITDA":        [float("nan"), float("nan")],
            },
            ["2025-12-31", "2025-09-30"],
        )
        source = FakeFundamentalsSource(quarterly=(df, "INR"))
        with pytest.raises(FinancialsFetchPermanent, match="no quarters"):
            fetch_quarterly_financials("X", "NSE", source=source)

    def test_rate_limit_classification(self):
        def boom(_):
            raise Exception("Rate limit exceeded")
        source = FakeFundamentalsSource(quarterly=boom)
        with pytest.raises(FinancialsFetchRateLimited):
            fetch_quarterly_financials("X", "NSE", source=source)

    def test_timeout_classified_as_transient(self):
        def boom(_):
            raise Exception("Read timed out")
        source = FakeFundamentalsSource(quarterly=boom)
        with pytest.raises(FinancialsFetchTransient):
            fetch_quarterly_financials("X", "NSE", source=source)

    def test_404_classified_as_permanent(self):
        def boom(_):
            raise Exception("HTTP 404 Symbol may be delisted")
        source = FakeFundamentalsSource(quarterly=boom)
        with pytest.raises(FinancialsFetchPermanent):
            fetch_quarterly_financials("X", "NSE", source=source)


# ---------- exception hierarchy ---------- #


def test_exception_hierarchy():
    assert issubclass(FinancialsFetchRateLimited, FinancialsFetchError)
    assert issubclass(FinancialsFetchTransient, FinancialsFetchError)
    assert issubclass(FinancialsFetchPermanent, FinancialsFetchError)


# ---------- symbol formatting ---------- #


def test_calls_yfinance_with_NSE_suffix():
    captured = []
    df = _make_df({"Total Revenue": [100]}, ["2025-12-31"])
    def capture(yf_symbol):
        captured.append(yf_symbol)
        return df, "INR"
    source = FakeFundamentalsSource(quarterly=capture)
    fetch_quarterly_financials("RELIANCE", "NSE", source=source)
    assert captured == ["RELIANCE.NS"]


def test_calls_yfinance_with_BSE_suffix():
    captured = []
    df = _make_df({"Total Revenue": [100]}, ["2025-12-31"])
    def capture(yf_symbol):
        captured.append(yf_symbol)
        return df, "INR"
    source = FakeFundamentalsSource(quarterly=capture)
    fetch_quarterly_financials("RELIANCE", "BSE", source=source)
    assert captured == ["RELIANCE.BO"]
