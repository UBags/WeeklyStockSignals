"""Tests for the yfinance historical mcap fetcher.

Patches trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical
to return (prices_df, shares_series) tuples. Uses real pandas
DataFrames/Series because the fetcher does DataFrame-shaped
operations.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from trip_ema.fundamentals.market_cap_fetch import (
    HistoricalMcapFetchError,
    HistoricalMcapFetchPermanent,
    HistoricalMcapFetchRateLimited,
    HistoricalMcapFetchTransient,
    HistoricalMcapPoint,
    fetch_historical_mcap_for_ticker,
)


# ---------- helpers ---------- #


def _prices_df(date_to_close: dict[str, float]) -> pd.DataFrame:
    """Build a DataFrame in the shape Ticker.history() returns:
    DatetimeIndex with Open/High/Low/Close/Volume columns."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in date_to_close])
    closes = list(date_to_close.values())
    return pd.DataFrame(
        {
            "Open":   closes,
            "High":   closes,
            "Low":    closes,
            "Close":  closes,
            "Volume": [0] * len(closes),
        },
        index=idx,
    )


def _shares_series(date_to_shares: dict[str, int]) -> pd.Series:
    """Build a Series in the shape get_shares_full() returns:
    DatetimeIndex → int values."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in date_to_shares])
    return pd.Series(list(date_to_shares.values()), index=idx)


# ---------- happy paths ---------- #


class TestHistoricalMcapHappyPath:

    def test_simple_one_year_range(self):
        """3 price dates + 1 shares row covering them. Each price
        date should produce one HistoricalMcapPoint."""
        prices = _prices_df({
            "2025-06-01": 1300.0,
            "2025-09-01": 1400.0,
            "2025-12-01": 1500.0,
        })
        shares = _shares_series({
            "2025-05-01": 6_700_000_000,
        })
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(prices, shares),
        ):
            points = fetch_historical_mcap_for_ticker(
                "RELIANCE", "NSE",
                from_date=date(2025, 1, 1),
                to_date=date(2026, 1, 1),
            )
        assert len(points) == 3
        assert points[0] == HistoricalMcapPoint(
            trade_date=date(2025, 6, 1),
            close_price=1300.0,
            shares_outstanding=6_700_000_000,
        )

    def test_shares_change_within_range(self):
        """Shares change mid-period (e.g. bonus issue). Each price
        date should use the shares value asof-or-earlier."""
        prices = _prices_df({
            "2025-06-01": 100.0,
            "2025-07-01": 100.0,
            "2025-08-01": 100.0,
            "2025-09-01": 100.0,
        })
        # Bonus issue 1:1 on July 15 doubles the shares.
        shares = _shares_series({
            "2025-05-01": 1_000_000,
            "2025-07-15": 2_000_000,
        })
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(prices, shares),
        ):
            points = fetch_historical_mcap_for_ticker(
                "X", "NSE",
                from_date=date(2025, 1, 1),
                to_date=date(2025, 12, 31),
            )
        # June and early-July use 1M shares; Aug and Sep use 2M.
        by_date = {p.trade_date: p.shares_outstanding for p in points}
        assert by_date[date(2025, 6, 1)] == 1_000_000
        assert by_date[date(2025, 7, 1)] == 1_000_000
        assert by_date[date(2025, 8, 1)] == 2_000_000
        assert by_date[date(2025, 9, 1)] == 2_000_000

    def test_range_filter_drops_outside_dates(self):
        """Prices DF has more dates than requested. Only dates
        within [from_date, to_date] should appear in output."""
        prices = _prices_df({
            "2024-12-01": 100.0,
            "2025-06-01": 100.0,
            "2026-01-01": 100.0,
        })
        shares = _shares_series({"2024-01-01": 1_000})
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(prices, shares),
        ):
            points = fetch_historical_mcap_for_ticker(
                "X", "NSE",
                from_date=date(2025, 1, 1),
                to_date=date(2025, 12, 31),
            )
        dates = [p.trade_date for p in points]
        assert dates == [date(2025, 6, 1)]

    def test_pre_shares_history_dates_dropped(self):
        """Price dates BEFORE the earliest shares snapshot should
        not produce points — we don't extrapolate backwards."""
        prices = _prices_df({
            "2024-01-01": 100.0,  # before any shares data
            "2025-06-01": 100.0,  # after first shares snapshot
        })
        shares = _shares_series({"2025-01-01": 1_000_000})
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(prices, shares),
        ):
            points = fetch_historical_mcap_for_ticker(
                "X", "NSE",
                from_date=date(2023, 1, 1),
                to_date=date(2026, 1, 1),
            )
        dates = [p.trade_date for p in points]
        assert dates == [date(2025, 6, 1)]

    def test_points_returned_in_ascending_date_order(self):
        """Whatever order yfinance returns rows, our output is
        ascending — natural order for backfill."""
        # Build prices in REVERSE order to verify sorting happens.
        prices = _prices_df({
            "2025-12-01": 300.0,
            "2025-06-01": 100.0,
            "2025-09-01": 200.0,
        })
        shares = _shares_series({"2025-01-01": 1_000})
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(prices, shares),
        ):
            points = fetch_historical_mcap_for_ticker(
                "X", "NSE",
                from_date=date(2025, 1, 1),
                to_date=date(2025, 12, 31),
            )
        dates = [p.trade_date for p in points]
        assert dates == sorted(dates)


# ---------- coercion / NaN handling ---------- #


class TestCoercion:

    def test_nan_close_dropped(self):
        """Pandas NaN in Close column means "missing" — that date
        is dropped, not emitted as a zero or NaN HistoricalMcapPoint."""
        prices = _prices_df({
            "2025-06-01": float("nan"),
            "2025-07-01": 100.0,
        })
        shares = _shares_series({"2025-01-01": 1_000})
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(prices, shares),
        ):
            points = fetch_historical_mcap_for_ticker(
                "X", "NSE",
                from_date=date(2025, 1, 1),
                to_date=date(2025, 12, 31),
            )
        dates = [p.trade_date for p in points]
        assert dates == [date(2025, 7, 1)]

    def test_zero_close_dropped(self):
        """Zero close is treated as missing — corporate action /
        bad data; we don't write market_cap=0 from that."""
        prices = _prices_df({
            "2025-06-01": 0.0,
            "2025-07-01": 100.0,
        })
        shares = _shares_series({"2025-01-01": 1_000})
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(prices, shares),
        ):
            points = fetch_historical_mcap_for_ticker(
                "X", "NSE",
                from_date=date(2025, 1, 1),
                to_date=date(2025, 12, 31),
            )
        dates = [p.trade_date for p in points]
        assert dates == [date(2025, 7, 1)]

    def test_zero_shares_skipped_from_series(self):
        """If get_shares_full returns a row with shares=0, that
        snapshot is ignored — could mean data corruption or pre-IPO."""
        prices = _prices_df({"2025-07-01": 100.0})
        # Two shares entries: a zero one followed by a real one.
        shares = pd.Series(
            [0, 1_000_000],
            index=pd.DatetimeIndex([
                pd.Timestamp("2025-01-01"),
                pd.Timestamp("2025-06-01"),
            ]),
        )
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(prices, shares),
        ):
            points = fetch_historical_mcap_for_ticker(
                "X", "NSE",
                from_date=date(2025, 1, 1),
                to_date=date(2025, 12, 31),
            )
        # Should use the 1M entry (the zero one is filtered out).
        assert len(points) == 1
        assert points[0].shares_outstanding == 1_000_000


# ---------- failure paths ---------- #


class TestFailures:

    def test_empty_prices_df_raises_permanent(self):
        empty_prices = pd.DataFrame()
        shares = _shares_series({"2025-01-01": 1_000})
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(empty_prices, shares),
        ):
            with pytest.raises(HistoricalMcapFetchPermanent, match="no price history"):
                fetch_historical_mcap_for_ticker(
                    "X", "NSE",
                    from_date=date(2025, 1, 1),
                    to_date=date(2025, 12, 31),
                )

    def test_empty_shares_series_raises_permanent(self):
        prices = _prices_df({"2025-06-01": 100.0})
        empty_shares = pd.Series([], dtype=float)
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(prices, empty_shares),
        ):
            with pytest.raises(HistoricalMcapFetchPermanent, match="get_shares_full"):
                fetch_historical_mcap_for_ticker(
                    "X", "NSE",
                    from_date=date(2025, 1, 1),
                    to_date=date(2025, 12, 31),
                )

    def test_no_overlap_raises_permanent(self):
        """Prices and shares are both present but no price date
        survives both the range filter and the asof-shares filter."""
        prices = _prices_df({"2023-01-01": 100.0})  # before range
        shares = _shares_series({"2025-01-01": 1_000})
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            return_value=(prices, shares),
        ):
            with pytest.raises(HistoricalMcapFetchPermanent, match="no trade dates"):
                fetch_historical_mcap_for_ticker(
                    "X", "NSE",
                    from_date=date(2025, 1, 1),
                    to_date=date(2025, 12, 31),
                )

    def test_rate_limit_classified(self):
        def boom(yf_sym, *, period):
            raise Exception("Rate limit exceeded")
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            side_effect=boom,
        ):
            with pytest.raises(HistoricalMcapFetchRateLimited):
                fetch_historical_mcap_for_ticker(
                    "X", "NSE",
                    from_date=date(2025, 1, 1),
                    to_date=date(2025, 12, 31),
                )

    def test_timeout_classified_as_transient(self):
        def boom(yf_sym, *, period):
            raise Exception("Read timed out")
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            side_effect=boom,
        ):
            with pytest.raises(HistoricalMcapFetchTransient):
                fetch_historical_mcap_for_ticker(
                    "X", "NSE",
                    from_date=date(2025, 1, 1),
                    to_date=date(2025, 12, 31),
                )

    def test_404_classified_as_permanent(self):
        def boom(yf_sym, *, period):
            raise Exception("HTTP 404 Symbol may be delisted")
        with patch(
            "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
            side_effect=boom,
        ):
            with pytest.raises(HistoricalMcapFetchPermanent):
                fetch_historical_mcap_for_ticker(
                    "X", "NSE",
                    from_date=date(2025, 1, 1),
                    to_date=date(2025, 12, 31),
                )


# ---------- exception hierarchy ---------- #


def test_exception_hierarchy():
    assert issubclass(HistoricalMcapFetchRateLimited, HistoricalMcapFetchError)
    assert issubclass(HistoricalMcapFetchTransient, HistoricalMcapFetchError)
    assert issubclass(HistoricalMcapFetchPermanent, HistoricalMcapFetchError)


# ---------- symbol formatting ---------- #


def test_calls_yfinance_with_NSE_suffix():
    captured = []
    prices = _prices_df({"2025-06-01": 100.0})
    shares = _shares_series({"2025-01-01": 1_000})

    def capture(yf_sym, *, period):
        captured.append((yf_sym, period))
        return prices, shares

    with patch(
        "trip_ema.fundamentals.market_cap_fetch._fetch_yfinance_historical",
        side_effect=capture,
    ):
        fetch_historical_mcap_for_ticker(
            "RELIANCE", "NSE",
            from_date=date(2025, 1, 1),
            to_date=date(2025, 12, 31),
        )
    assert captured[0][0] == "RELIANCE.NS"
