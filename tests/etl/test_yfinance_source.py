"""Tests for the yfinance source. We mock yfinance entirely — these
tests verify our DataFrame normalization and batching logic, not
yfinance's behavior."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from trip_ema.etl.yfinance_source import (
    YFinanceSource,
    from_yfinance_symbol,
    to_yfinance_symbol,
)


class TestSymbolMapping:

    def test_to_yfinance_appends_ns(self):
        assert to_yfinance_symbol("RELIANCE") == "RELIANCE.NS"
        assert to_yfinance_symbol("tcs") == "TCS.NS"

    def test_from_yfinance_strips_suffix(self):
        assert from_yfinance_symbol("RELIANCE.NS") == "RELIANCE"
        assert from_yfinance_symbol("TCS") == "TCS"

    def test_round_trip(self):
        for s in ["RELIANCE", "TCS", "ICICIBANK", "AXISBANK"]:
            assert from_yfinance_symbol(to_yfinance_symbol(s)) == s


def _build_multi_df(symbol_data: dict):
    """Build a pandas multi-index DataFrame that mimics what yfinance
    returns for yf.download(['A.NS', 'B.NS'], ...). Each symbol's data
    is a dict of {col: list_of_values}; index is shared.
    """
    import pandas as pd
    cols = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    # Common date index
    first_data = next(iter(symbol_data.values()))
    n = len(first_data["Open"])
    idx = pd.DatetimeIndex([
        pd.Timestamp(2026, 1, 1) + pd.Timedelta(days=i) for i in range(n)
    ])
    # Build columns as multi-index (symbol, field)
    arrays = []
    for sym in symbol_data:
        for c in cols:
            arrays.append((sym, c))
    multi_cols = pd.MultiIndex.from_tuples(arrays)
    data = {}
    for sym, fields in symbol_data.items():
        for c in cols:
            data[(sym, c)] = fields.get(c, [0.0] * n)
    df = pd.DataFrame(data, index=idx, columns=multi_cols)
    return df


def _build_flat_df(fields: dict):
    """Build a flat DataFrame as yfinance returns for a single-symbol
    yf.download(). No multi-index."""
    import pandas as pd
    cols = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    n = len(fields["Open"])
    idx = pd.DatetimeIndex([
        pd.Timestamp(2026, 1, 1) + pd.Timedelta(days=i) for i in range(n)
    ])
    return pd.DataFrame({c: fields.get(c, [0.0] * n) for c in cols}, index=idx)


class TestNormalization:

    @patch("yfinance.download")
    def test_two_symbols_both_succeed(self, mock_download):
        mock_download.return_value = _build_multi_df({
            "RELIANCE.NS": {
                "Open": [100.0, 101.0],
                "High": [102.0, 103.0],
                "Low": [99.0, 100.0],
                "Close": [101.5, 102.5],
                "Adj Close": [101.5, 102.5],
                "Volume": [1000.0, 1100.0],
            },
            "TCS.NS": {
                "Open": [3000.0, 3010.0],
                "High": [3020.0, 3030.0],
                "Low": [2990.0, 3000.0],
                "Close": [3015.0, 3025.0],
                "Adj Close": [3015.0, 3025.0],
                "Volume": [500.0, 550.0],
            },
        })
        src = YFinanceSource()
        results = src.fetch_daily_history(
            ["RELIANCE", "TCS"], start=date(2026, 1, 1),
        )
        assert "RELIANCE" in results and "TCS" in results
        # Both succeed
        assert results["RELIANCE"].succeeded
        assert results["TCS"].succeeded
        # 2 bars each
        assert len(results["RELIANCE"].bars) == 2
        assert results["RELIANCE"].bars[0].close == 101.5
        assert results["TCS"].bars[1].close == 3025.0

    @patch("yfinance.download")
    def test_missing_symbol_returns_error_outcome(self, mock_download):
        """yfinance returns a DataFrame missing one of the requested
        symbols — we report an error for it but succeed on the other."""
        mock_download.return_value = _build_multi_df({
            "RELIANCE.NS": {
                "Open": [100.0], "High": [101.0], "Low": [99.0],
                "Close": [100.5], "Adj Close": [100.5], "Volume": [1000.0],
            },
            # TCS.NS not in the response
        })
        src = YFinanceSource()
        results = src.fetch_daily_history(
            ["RELIANCE", "TCS"], start=date(2026, 1, 1),
        )
        assert results["RELIANCE"].succeeded
        assert not results["TCS"].succeeded
        assert "not in returned columns" in results["TCS"].error_message

    @patch("yfinance.download")
    def test_nan_rows_are_skipped_not_written(self, mock_download):
        # A symbol whose first row is all NaN (missing day) — should
        # be skipped in the bars output.
        mock_download.return_value = _build_flat_df({
            "Open": [float("nan"), 101.0],
            "High": [float("nan"), 103.0],
            "Low": [float("nan"), 100.0],
            "Close": [float("nan"), 102.5],
            "Adj Close": [float("nan"), 102.5],
            "Volume": [float("nan"), 1100.0],
        })
        src = YFinanceSource()
        results = src.fetch_daily_history(["RELIANCE"], start=date(2026, 1, 1))
        # Only the non-NaN bar
        assert len(results["RELIANCE"].bars) == 1
        assert results["RELIANCE"].bars[0].close == 102.5

    @patch("yfinance.download")
    def test_empty_dataframe_all_symbols_fail(self, mock_download):
        import pandas as pd
        mock_download.return_value = pd.DataFrame()
        src = YFinanceSource()
        results = src.fetch_daily_history(
            ["RELIANCE", "TCS"], start=date(2026, 1, 1),
        )
        for s in ["RELIANCE", "TCS"]:
            assert not results[s].succeeded
            assert "empty" in results[s].error_message.lower()


class TestRetryAndFailure:

    @patch("yfinance.download")
    def test_all_retries_failing_yields_error_outcomes(self, mock_download):
        mock_download.side_effect = RuntimeError("simulated yfinance error")
        src = YFinanceSource(max_retries=2, retry_backoff_base=0.0)
        results = src.fetch_daily_history(["RELIANCE"], start=date(2026, 1, 1))
        assert not results["RELIANCE"].succeeded
        assert "simulated yfinance error" in results["RELIANCE"].error_message
        # Verify it retried twice
        assert mock_download.call_count == 2

    @patch("yfinance.download")
    def test_retry_succeeds_after_initial_failure(self, mock_download):
        good_df = _build_flat_df({
            "Open": [100.0], "High": [101.0], "Low": [99.0],
            "Close": [100.5], "Adj Close": [100.5], "Volume": [1000.0],
        })
        mock_download.side_effect = [RuntimeError("first try fails"), good_df]
        src = YFinanceSource(max_retries=3, retry_backoff_base=0.0)
        results = src.fetch_daily_history(["RELIANCE"], start=date(2026, 1, 1))
        assert results["RELIANCE"].succeeded
        assert len(results["RELIANCE"].bars) == 1
        assert mock_download.call_count == 2


class TestBatching:

    @patch("yfinance.download")
    def test_large_list_is_split_into_batches(self, mock_download):
        # 75 symbols, batch_size=50 → 2 batches
        def fake_download(tickers=None, **kwargs):
            return _build_multi_df({
                t: {
                    "Open": [100.0], "High": [101.0], "Low": [99.0],
                    "Close": [100.5], "Adj Close": [100.5], "Volume": [1000.0],
                }
                for t in tickers
            })
        mock_download.side_effect = fake_download
        src = YFinanceSource(batch_size=50)
        symbols = [f"SYM{i}" for i in range(75)]
        results = src.fetch_daily_history(symbols, start=date(2026, 1, 1))
        assert len(results) == 75
        # 2 batches expected
        assert mock_download.call_count == 2
        # First batch had 50 tickers
        first_call_kwargs = mock_download.call_args_list[0].kwargs
        assert len(first_call_kwargs["tickers"]) == 50
        # Second batch had 25
        second_call_kwargs = mock_download.call_args_list[1].kwargs
        assert len(second_call_kwargs["tickers"]) == 25
