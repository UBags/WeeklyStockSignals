"""Tests for the yfinance shares-outstanding fetcher.

The fetcher takes a FundamentalsSource (trip_ema/fundamentals/ports.py); these
tests inject a FakeFundamentalsSource. Nothing is patched, and none of these
tests require yfinance to be installed.
"""
from __future__ import annotations


import pytest

from .fakes import FakeFundamentalsSource
from trip_ema.fundamentals.shares_fetch import (
    SharesFetchError,
    SharesFetchPermanent,
    SharesFetchRateLimited,
    SharesFetchTransient,
    SharesInfo,
    fetch_shares,
)


# ---------- happy paths ---------- #


def test_returns_direct_shares_outstanding():
    """The clean case: yfinance has sharesOutstanding populated."""
    mock_info = {
        "symbol": "RELIANCE.NS",
        "shortName": "Reliance",
        "sharesOutstanding": 6_768_140_000,
        "quoteType": "EQUITY",
    }
    source = FakeFundamentalsSource(info=mock_info)
    result = fetch_shares("RELIANCE", "NSE", source=source)
    assert result == SharesInfo(
        shares_outstanding=6_768_140_000,
        source="YFINANCE",
    )


def test_falls_back_to_implied_when_direct_missing():
    """sharesOutstanding NULL but impliedSharesOutstanding populated
    → use the implied value, record source as YFINANCE_IMPLIED."""
    mock_info = {
        "symbol": "MIDCAP.NS",
        "shortName": "Some Mid Cap",
        "impliedSharesOutstanding": 425_000_000,
        # no sharesOutstanding key
    }
    source = FakeFundamentalsSource(info=mock_info)
    result = fetch_shares("MIDCAP", "NSE", source=source)
    assert result.shares_outstanding == 425_000_000
    assert result.source == "YFINANCE_IMPLIED"


def test_prefers_direct_when_both_present():
    """Both fields populated → direct wins. The implied is a
    derivative, so when we have the source-of-truth direct number
    we don't need to second-guess it."""
    mock_info = {
        "symbol": "X.NS",
        "shortName": "X",
        "sharesOutstanding": 1_000_000_000,
        "impliedSharesOutstanding": 1_001_000_000,  # slightly different
    }
    source = FakeFundamentalsSource(info=mock_info)
    result = fetch_shares("X", "NSE", source=source)
    assert result.shares_outstanding == 1_000_000_000
    assert result.source == "YFINANCE"


# ---------- yfinance quirks ---------- #


def test_zero_shares_treated_as_missing():
    """yfinance returns 0 for some delisted/unknown tickers — treat
    as missing rather than recording a literally-zero shares count."""
    mock_info = {
        "symbol": "X.NS",
        "shortName": "X",
        "sharesOutstanding": 0,
        "impliedSharesOutstanding": 100_000,
    }
    source = FakeFundamentalsSource(info=mock_info)
    result = fetch_shares("X", "NSE", source=source)
    # Direct=0 dismissed; falls back to implied.
    assert result.shares_outstanding == 100_000
    assert result.source == "YFINANCE_IMPLIED"


def test_negative_shares_treated_as_missing():
    """Defensive: a negative shares count is nonsensical. Treat as
    missing rather than recording garbage."""
    mock_info = {
        "symbol": "X.NS",
        "shortName": "X",
        "sharesOutstanding": -1,
        "impliedSharesOutstanding": 500,
    }
    source = FakeFundamentalsSource(info=mock_info)
    result = fetch_shares("X", "NSE", source=source)
    assert result.shares_outstanding == 500
    assert result.source == "YFINANCE_IMPLIED"


def test_float_shares_converted_to_int():
    """yfinance has been observed to return shares as floats in some
    contexts (e.g. JSON encoding). Convert to int."""
    mock_info = {
        "symbol": "X.NS",
        "shortName": "X",
        "sharesOutstanding": 1.234e9,
    }
    source = FakeFundamentalsSource(info=mock_info)
    result = fetch_shares("X", "NSE", source=source)
    assert result.shares_outstanding == 1_234_000_000
    assert isinstance(result.shares_outstanding, int)


def test_string_shares_unparseable_treated_as_missing():
    """If yfinance returns a weird string, fall back to implied."""
    mock_info = {
        "symbol": "X.NS",
        "shortName": "X",
        "sharesOutstanding": "N/A",
        "impliedSharesOutstanding": 500,
    }
    source = FakeFundamentalsSource(info=mock_info)
    result = fetch_shares("X", "NSE", source=source)
    assert result.shares_outstanding == 500


# ---------- failure paths ---------- #


def test_empty_info_dict_raises_permanent():
    """Delisted/nonexistent tickers come back with {}."""
    source = FakeFundamentalsSource(info={})
    with pytest.raises(SharesFetchPermanent, match="empty info"):
        fetch_shares("DELISTED", "NSE", source=source)


def test_neither_field_populated_raises_permanent():
    """Valid ticker but no shares data at all — nothing to retry on,
    so SharesFetchPermanent, not Transient."""
    mock_info = {
        "symbol": "X.NS",
        "shortName": "X",
        "quoteType": "EQUITY",
        # no sharesOutstanding, no impliedSharesOutstanding
    }
    source = FakeFundamentalsSource(info=mock_info)
    with pytest.raises(SharesFetchPermanent, match="neither"):
        fetch_shares("X", "NSE", source=source)


def test_both_fields_zero_raises_permanent():
    """Both fields present but both zero — same outcome as both
    missing."""
    mock_info = {
        "symbol": "X.NS",
        "shortName": "X",
        "sharesOutstanding": 0,
        "impliedSharesOutstanding": 0,
    }
    source = FakeFundamentalsSource(info=mock_info)
    with pytest.raises(SharesFetchPermanent):
        fetch_shares("X", "NSE", source=source)


def test_rate_limit_classification():
    """yfinance rate-limit error → SharesFetchRateLimited, distinct
    from other failures so the runner can apply the longer cool-off."""
    def raise_rate_limit(_):
        raise Exception("Rate limit exceeded for this endpoint")

    source = FakeFundamentalsSource(info=raise_rate_limit)
    with pytest.raises(SharesFetchRateLimited):
        fetch_shares("X", "NSE", source=source)


def test_timeout_classified_as_transient():
    def raise_timeout(_):
        raise Exception("HTTPSConnectionPool: Read timed out.")

    source = FakeFundamentalsSource(info=raise_timeout)
    with pytest.raises(SharesFetchTransient):
        fetch_shares("X", "NSE", source=source)


def test_404_classified_as_permanent():
    def raise_404(_):
        raise Exception("HTTP 404: Symbol may be delisted")

    source = FakeFundamentalsSource(info=raise_404)
    with pytest.raises(SharesFetchPermanent):
        fetch_shares("X", "NSE", source=source)


def test_unknown_error_classified_as_transient():
    """Defensive: unknown yfinance error messages don't permanently
    park tickers. The next refresh will pick them up."""
    def raise_weird(_):
        raise Exception("Some new error message yfinance just invented")

    source = FakeFundamentalsSource(info=raise_weird)
    with pytest.raises(SharesFetchTransient):
        fetch_shares("X", "NSE", source=source)


# ---------- exception hierarchy ---------- #


def test_all_specific_exceptions_inherit_from_base():
    """Runner catches SharesFetchError to count any failure;
    subtypes refine that. Tests pin the contract."""
    assert issubclass(SharesFetchRateLimited, SharesFetchError)
    assert issubclass(SharesFetchTransient, SharesFetchError)
    assert issubclass(SharesFetchPermanent, SharesFetchError)


# ---------- symbol formatting ---------- #


def test_calls_yfinance_with_NSE_suffix():
    """NSE → '.NS' suffix. Mirrors the industry_fetch test."""
    captured = []

    def capture_arg(yf_symbol):
        captured.append(yf_symbol)
        return {"symbol": yf_symbol, "shortName": "x",
                "sharesOutstanding": 1}

    source = FakeFundamentalsSource(info=capture_arg)
    fetch_shares("RELIANCE", "NSE", source=source)
    assert captured == ["RELIANCE.NS"]


def test_calls_yfinance_with_BSE_suffix():
    captured = []

    def capture_arg(yf_symbol):
        captured.append(yf_symbol)
        return {"symbol": yf_symbol, "shortName": "x",
                "sharesOutstanding": 1}

    source = FakeFundamentalsSource(info=capture_arg)
    fetch_shares("RELIANCE", "BSE", source=source)
    assert captured == ["RELIANCE.BO"]


def test_calls_yfinance_with_no_suffix_for_NYSE():
    captured = []

    def capture_arg(yf_symbol):
        captured.append(yf_symbol)
        return {"symbol": yf_symbol, "shortName": "x",
                "sharesOutstanding": 1}

    source = FakeFundamentalsSource(info=capture_arg)
    fetch_shares("IBM", "NYSE", source=source)
    assert captured == ["IBM"]
