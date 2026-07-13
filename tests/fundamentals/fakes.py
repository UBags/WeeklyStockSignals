"""Test doubles for the fundamentals ports.

`FakeFundamentalsSource` replaces what used to be a `unittest.mock.patch` of
`trip_ema.fundamentals.<module>._fetch_yfinance_*`. Each method is programmed
with a single value, and the same value can mean three things — which is
exactly the return_value/side_effect distinction the patches used, collapsed
into one argument:

    FakeFundamentalsSource(info={"symbol": "X.NS"})       -> returned
    FakeFundamentalsSource(info=Exception("Rate limit"))  -> raised
    FakeFundamentalsSource(info=lambda sym: ...)          -> called

Calls are recorded, so a test that wants to assert the symbol the fetcher built
reads `source.info_calls == ["RELIANCE.NS"]` instead of interrogating a mock.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def _resolve(value: Any, *args: Any, **kwargs: Any) -> Any:
    """Exception -> raise. Callable -> call. Anything else -> return."""
    if isinstance(value, BaseException):
        raise value
    if isinstance(value, type) and issubclass(value, BaseException):
        raise value()
    if callable(value):
        return value(*args, **kwargs)
    return value


class FakeFundamentalsSource:
    """A FundamentalsSource whose every method is programmable.

    Any method left unprogrammed raises AssertionError if called — a test that
    trips that is asking the source for something it never said it wanted, and
    silently returning None would hide it.
    """

    _UNSET = object()

    def __init__(
        self,
        *,
        info: Any = _UNSET,
        quarterly: Any = _UNSET,
        history: Any = _UNSET,
        shares_full: Any = _UNSET,
    ) -> None:
        self._info = info
        self._quarterly = quarterly
        self._history = history
        self._shares_full = shares_full
        self.info_calls: list[str] = []
        self.quarterly_calls: list[str] = []
        self.history_calls: list[tuple[str, str]] = []
        self.shares_full_calls: list[tuple[str, date]] = []

    def _require(self, value: Any, name: str) -> Any:
        if value is self._UNSET:
            raise AssertionError(
                f"FakeFundamentalsSource.{name}() called but no `{name}` was "
                f"configured on this fake"
            )
        return value

    def ticker_info(self, yf_symbol: str) -> dict[str, Any]:
        self.info_calls.append(yf_symbol)
        return _resolve(self._require(self._info, "info"), yf_symbol)

    def quarterly_financials(self, yf_symbol: str) -> tuple[Any, str]:
        self.quarterly_calls.append(yf_symbol)
        return _resolve(self._require(self._quarterly, "quarterly"), yf_symbol)

    def price_and_shares_history(
        self, yf_symbol: str, *, period: str,
    ) -> tuple[Any, Any]:
        self.history_calls.append((yf_symbol, period))
        return _resolve(
            self._require(self._history, "history"), yf_symbol, period=period,
        )

    def shares_full(self, yf_symbol: str, start_date: date) -> Any:
        self.shares_full_calls.append((yf_symbol, start_date))
        return _resolve(
            self._require(self._shares_full, "shares_full"), yf_symbol, start_date,
        )
