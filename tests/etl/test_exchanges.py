"""Tests for trip_ema.etl.exchanges.

Phase 14.6a — covers:
  - Suffix-map correctness for all 9 known exchanges
  - yfinance_symbol formatting (single + round-trip)
  - from_yfinance_symbol inverse + fallback for unknown suffixes
  - UnsupportedExchangeError on unknown exchanges
  - validate_exchange's two-step check (known + configured)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trip_ema.etl.exchanges import (
    UnsupportedExchangeError,
    configured_exchanges,
    from_yfinance_symbol,
    known_exchanges,
    validate_exchange,
    yfinance_symbol,
)


# ---------- yfinance_symbol ---------- #


class TestYfinanceSymbol:

    @pytest.mark.parametrize(
        "exchange, bare, expected",
        [
            ("NSE",    "RELIANCE",  "RELIANCE.NS"),
            ("BSE",    "RELIANCE",  "RELIANCE.BO"),
            ("NYSE",   "IBM",       "IBM"),
            ("NASDAQ", "MSFT",      "MSFT"),
            ("TSE",    "7203",      "7203.T"),
            ("HKSE",   "0700",      "0700.HK"),
            ("SSE",    "600519",    "600519.SS"),
            ("SZSE",   "000001",    "000001.SZ"),
            ("LSE",    "BARC",      "BARC.L"),
        ],
    )
    def test_known_exchange_formats_correctly(self, exchange, bare, expected):
        assert yfinance_symbol(bare, exchange) == expected

    def test_uppercases_input(self):
        assert yfinance_symbol("tcs", "NSE") == "TCS.NS"
        assert yfinance_symbol("aapl", "NYSE") == "AAPL"

    def test_unknown_exchange_raises(self):
        with pytest.raises(UnsupportedExchangeError) as ei:
            yfinance_symbol("FOO", "XYZ")
        msg = str(ei.value).lower()
        assert "xyz" in msg
        assert "known" in msg


# ---------- from_yfinance_symbol ---------- #


class TestFromYfinanceSymbol:

    @pytest.mark.parametrize(
        "exchange, yf, expected",
        [
            ("NSE",    "RELIANCE.NS", "RELIANCE"),
            ("BSE",    "RELIANCE.BO", "RELIANCE"),
            ("NYSE",   "IBM",         "IBM"),
            ("NASDAQ", "MSFT",        "MSFT"),
            ("TSE",    "7203.T",      "7203"),
            ("HKSE",   "0700.HK",     "0700"),
            ("LSE",    "BARC.L",      "BARC"),
        ],
    )
    def test_strips_known_suffix(self, exchange, yf, expected):
        assert from_yfinance_symbol(yf, exchange) == expected

    def test_fallback_when_suffix_doesnt_match(self):
        # Caller asked for NSE but the symbol has a BSE suffix.
        # The function falls back to "strip after first dot" behaviour.
        assert from_yfinance_symbol("RELIANCE.BO", "NSE") == "RELIANCE"

    def test_round_trip(self):
        for ex in ("NSE", "BSE", "NYSE", "TSE", "HKSE", "LSE"):
            for sym in ("RELIANCE", "TCS", "AAPL", "MSFT"):
                assert (
                    from_yfinance_symbol(yfinance_symbol(sym, ex), ex)
                    == sym
                )

    def test_unknown_exchange_raises(self):
        with pytest.raises(UnsupportedExchangeError):
            from_yfinance_symbol("FOO.X", "XYZ")


# ---------- known_exchanges ---------- #


class TestKnownExchanges:

    def test_returns_frozenset(self):
        result = known_exchanges()
        assert isinstance(result, frozenset)

    def test_contains_all_nine(self):
        result = known_exchanges()
        expected = {
            "NSE", "BSE", "NYSE", "NASDAQ", "TSE",
            "HKSE", "SSE", "SZSE", "LSE",
        }
        assert result == expected


# ---------- configured_exchanges ---------- #


class TestConfiguredExchanges:

    def test_reads_market_hours(self):
        config = MagicMock()
        config.market_hours = [
            type("MH", (), {"exchange": "NSE"})(),
            type("MH", (), {"exchange": "BSE"})(),
        ]
        result = configured_exchanges(config)
        assert result == frozenset({"NSE", "BSE"})

    def test_empty_when_no_market_hours(self):
        config = MagicMock()
        config.market_hours = []
        assert configured_exchanges(config) == frozenset()

    def test_missing_attribute_is_empty(self):
        """If config doesn't even have market_hours (an unusual edge
        case for tests with bare mocks), don't crash — return empty."""
        # Use a plain class without the attribute
        class BareConfig:
            pass
        assert configured_exchanges(BareConfig()) == frozenset()


# ---------- validate_exchange ---------- #


class TestValidateExchange:

    def _config(self, exchanges):
        cfg = MagicMock()
        cfg.market_hours = [
            type("MH", (), {"exchange": e})() for e in exchanges
        ]
        return cfg

    def test_known_and_configured_passes(self):
        # No exception
        validate_exchange("NSE", self._config(["NSE"]))

    def test_unknown_exchange_raises(self):
        with pytest.raises(UnsupportedExchangeError) as ei:
            validate_exchange("XYZ", self._config(["NSE", "XYZ"]))
        msg = str(ei.value).lower()
        assert "not supported" in msg
        assert "xyz" in msg
        # The "known" listing should appear so operators can correct
        # the typo
        assert "known" in msg

    def test_known_but_not_configured_raises(self):
        with pytest.raises(UnsupportedExchangeError) as ei:
            validate_exchange("BSE", self._config(["NSE"]))
        msg = str(ei.value).lower()
        assert "not configured" in msg
        assert "bse" in msg
        # The configured-list should appear so operators see what IS
        # available
        assert "nse" in msg

    def test_empty_config_raises(self):
        with pytest.raises(UnsupportedExchangeError) as ei:
            validate_exchange("NSE", self._config([]))
        msg = str(ei.value).lower()
        assert "not configured" in msg

    def test_case_sensitive(self):
        """Exchange codes are case-sensitive — 'nse' is not 'NSE'."""
        with pytest.raises(UnsupportedExchangeError):
            validate_exchange("nse", self._config(["NSE"]))
