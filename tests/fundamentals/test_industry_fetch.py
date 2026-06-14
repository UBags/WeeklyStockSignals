"""Tests for the yfinance industry fetcher.

We patch trip_ema.fundamentals.industry_fetch._fetch_yfinance_info
rather than yfinance itself — that's the isolation point. None of
these tests require yfinance to be installed.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from trip_ema.fundamentals.industry_fetch import (
    IndustryFetchError,
    IndustryFetchPermanent,
    IndustryFetchRateLimited,
    IndustryFetchTransient,
    IndustryInfo,
    fetch_industry,
)


# ---------- happy paths ---------- #


def test_returns_sector_and_industry_for_valid_ticker():
    """The clean case: Yahoo gives us both fields."""
    mock_info = {
        "symbol": "RELIANCE.NS",
        "shortName": "Reliance Industries",
        "sector": "Energy",
        "industry": "Oil & Gas Refining & Marketing",
        "quoteType": "EQUITY",
    }
    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        return_value=mock_info,
    ):
        result = fetch_industry("RELIANCE", "NSE")
    assert result == IndustryInfo(
        sector="Energy",
        industry="Oil & Gas Refining & Marketing",
        quote_type="EQUITY",
    )


def test_yahoo_returns_only_sector_no_industry():
    """Common for small-cap Indian tickers. Not an error."""
    mock_info = {
        "symbol": "FOO.NS",
        "shortName": "Foo Industries",
        "sector": "Industrials",
        "quoteType": "EQUITY",
        # no 'industry' key
    }
    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        return_value=mock_info,
    ):
        result = fetch_industry("FOO", "NSE")
    assert result.sector == "Industrials"
    assert result.industry is None


def test_neither_sector_nor_industry_set():
    """Valid ticker, but yfinance has no classification. Not an error
    — we want a row with NULLs so the next refresh attempt doesn't
    re-do this work for no reason."""
    mock_info = {
        "symbol": "BAR.NS",
        "shortName": "Bar Holdings",
        "quoteType": "EQUITY",
    }
    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        return_value=mock_info,
    ):
        result = fetch_industry("BAR", "NSE")
    assert result == IndustryInfo(sector=None, industry=None, quote_type="EQUITY")


# ---------- yfinance quirks ---------- #


def test_strips_whitespace_in_fields():
    mock_info = {
        "symbol": "X.NS",
        "shortName": "X",
        "sector": "  Technology  ",
        "industry": "\tSoftware\n",
    }
    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        return_value=mock_info,
    ):
        result = fetch_industry("X", "NSE")
    assert result.sector == "Technology"
    assert result.industry == "Software"


def test_empty_string_becomes_none():
    mock_info = {
        "symbol": "X.NS",
        "shortName": "X",
        "sector": "",
        "industry": "  ",
    }
    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        return_value=mock_info,
    ):
        result = fetch_industry("X", "NSE")
    assert result.sector is None
    assert result.industry is None


def test_literal_string_none_becomes_none():
    """yfinance has been known to return the literal string 'None'
    when the upstream data source serialized a missing value badly."""
    mock_info = {
        "symbol": "X.NS",
        "shortName": "X",
        "sector": "None",
        "industry": "NONE",
    }
    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        return_value=mock_info,
    ):
        result = fetch_industry("X", "NSE")
    assert result.sector is None
    assert result.industry is None


# ---------- failure paths ---------- #


def test_empty_info_dict_raises_permanent():
    """Delisted or nonexistent tickers come back with {}."""
    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        return_value={},
    ):
        with pytest.raises(IndustryFetchPermanent, match="empty info"):
            fetch_industry("DELISTED", "NSE")


def test_info_dict_with_no_identifying_fields_raises_permanent():
    """yfinance has been seen to return junk dicts with no symbol
    or name. Treat as permanent failure."""
    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        return_value={"random_field": "value"},
    ):
        with pytest.raises(IndustryFetchPermanent):
            fetch_industry("WHATEVER", "NSE")


def test_rate_limit_classification():
    """When yfinance signals rate limit, we surface a distinct
    exception so the runner can apply a longer cool-off."""
    def raise_rate_limit(_):
        raise Exception("Rate limit exceeded for this endpoint")

    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        side_effect=raise_rate_limit,
    ):
        with pytest.raises(IndustryFetchRateLimited):
            fetch_industry("X", "NSE")


def test_timeout_classified_as_transient():
    def raise_timeout(_):
        raise Exception("HTTPSConnectionPool: Read timed out.")

    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        side_effect=raise_timeout,
    ):
        with pytest.raises(IndustryFetchTransient):
            fetch_industry("X", "NSE")


def test_404_classified_as_permanent():
    def raise_404(_):
        raise Exception("HTTP 404: Symbol may be delisted")

    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        side_effect=raise_404,
    ):
        with pytest.raises(IndustryFetchPermanent):
            fetch_industry("X", "NSE")


def test_unknown_error_classified_as_transient():
    """Defensive: unknown yfinance error messages don't permanently
    park tickers. The runner will retry on the next quarterly
    refresh."""
    def raise_weird(_):
        raise Exception("Some new error message yfinance just invented")

    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        side_effect=raise_weird,
    ):
        with pytest.raises(IndustryFetchTransient):
            fetch_industry("X", "NSE")


# ---------- exception hierarchy contract ---------- #


def test_all_specific_exceptions_inherit_from_base():
    """The runner catches IndustryFetchError to count any failure;
    the specific subtypes refine that. Tests pin the hierarchy
    contract."""
    assert issubclass(IndustryFetchRateLimited, IndustryFetchError)
    assert issubclass(IndustryFetchTransient, IndustryFetchError)
    assert issubclass(IndustryFetchPermanent, IndustryFetchError)


# ---------- symbol formatting ---------- #


def test_calls_yfinance_with_NSE_suffix():
    """The fetcher must apply the exchange suffix so yfinance gets
    the right symbol. NSE → '.NS'."""
    captured = []

    def capture_arg(yf_symbol):
        captured.append(yf_symbol)
        return {"symbol": yf_symbol, "shortName": "x"}

    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        side_effect=capture_arg,
    ):
        fetch_industry("RELIANCE", "NSE")
    assert captured == ["RELIANCE.NS"]


def test_calls_yfinance_with_BSE_suffix():
    captured = []

    def capture_arg(yf_symbol):
        captured.append(yf_symbol)
        return {"symbol": yf_symbol, "shortName": "x"}

    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        side_effect=capture_arg,
    ):
        fetch_industry("RELIANCE", "BSE")
    assert captured == ["RELIANCE.BO"]


def test_calls_yfinance_with_no_suffix_for_NYSE():
    captured = []

    def capture_arg(yf_symbol):
        captured.append(yf_symbol)
        return {"symbol": yf_symbol, "shortName": "x"}

    with patch(
        "trip_ema.fundamentals.industry_fetch._fetch_yfinance_info",
        side_effect=capture_arg,
    ):
        fetch_industry("IBM", "NYSE")
    assert captured == ["IBM"]
