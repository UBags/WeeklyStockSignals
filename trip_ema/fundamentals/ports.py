"""Ports for the outside world that `fundamentals` reads from.

Every module in this package that needed a number out of Yahoo used to reach
for `yfinance` directly, through a private `_fetch_yfinance_*` function whose
docstring said, in as many words, "isolated for test patchability". That is a
port with the type erased: the seam existed, but as a module global to be
monkeypatched rather than a collaborator to be passed in. This makes it a real
one.

`FundamentalsSource` is the interface the fetchers depend on. It is a
structural Protocol, not a base class — the adapter below satisfies it by
having the right methods, and so does any test double, with no inheritance and
no registration.

Two rules the fetchers rely on:

  * The source is DUMB. It does the network call and returns whatever the
    upstream gave back — a dict, a DataFrame, a Series. It does not interpret,
    validate, or classify. Every fetcher's `_classify_yfinance_exception` still
    owns the mapping from a raw exception to the typed Rate-limited / Transient
    / Permanent taxonomy, because that mapping is a domain decision about
    whether to retry, not a transport detail.

  * The source raises whatever the upstream raises. The one exception is
    `SourceCapabilityError`, below, for "this source cannot serve this call at
    all" — which is not a transient failure and must not be classified as one.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol, runtime_checkable


class SourceCapabilityError(RuntimeError):
    """The source cannot serve this call at all — e.g. the installed yfinance
    is too old to have `get_shares_full`.

    Distinct from a fetch failure: retrying will not help, and the message-
    substring classifiers would otherwise map it to Transient and re-attempt it
    forever. Fetchers catch this explicitly and raise their own Permanent.
    """


@runtime_checkable
class FundamentalsSource(Protocol):
    """What `fundamentals` needs from an upstream market-data provider.

    runtime_checkable so a test can assert an adapter really implements every
    method — isinstance() on a Protocol checks method presence, which is the
    whole contract here. It is not used for dispatch anywhere in production.
    """

    def ticker_info(self, yf_symbol: str) -> dict[str, Any]:
        """The provider's grab-bag info dict. `{}` if the symbol is unknown."""
        ...

    def quarterly_financials(self, yf_symbol: str) -> tuple[Any, str]:
        """(quarterly income-statement DataFrame, currency code)."""
        ...

    def price_and_shares_history(
        self, yf_symbol: str, *, period: str,
    ) -> tuple[Any, Any]:
        """(prices DataFrame indexed by date, shares Series indexed by date)."""
        ...

    def shares_full(self, yf_symbol: str, start_date: date) -> Any:
        """Shares-outstanding Series from `start_date` to now."""
        ...


class YFinanceFundamentalsSource:
    """The production adapter. Satisfies FundamentalsSource against yfinance.

    yfinance is imported lazily, per call, exactly as the `_fetch_yfinance_*`
    functions this replaces did: the package is only needed when an ETL run
    actually fires, and the test suite must not require it to be installed.
    """

    def ticker_info(self, yf_symbol: str) -> dict[str, Any]:
        import yfinance as yf

        return yf.Ticker(yf_symbol).info or {}

    def quarterly_financials(self, yf_symbol: str) -> tuple[Any, str]:
        import yfinance as yf

        ticker = yf.Ticker(yf_symbol)
        # `quarterly_income_stmt` is the newer name; `quarterly_financials` is
        # the alias in older yfinance. Prefer the new one, fall back if absent.
        df = getattr(ticker, "quarterly_income_stmt", None)
        if df is None or getattr(df, "empty", True):
            df = getattr(ticker, "quarterly_financials", None)

        # Currency lives in `info`. Default to INR — the common case for our
        # universe — and never let a currency lookup fail the whole fetch.
        currency = "INR"
        try:
            info = ticker.info or {}
            if info.get("financialCurrency"):
                currency = str(info["financialCurrency"])
        except Exception:
            pass
        return df, currency

    def price_and_shares_history(
        self, yf_symbol: str, *, period: str,
    ) -> tuple[Any, Any]:
        import yfinance as yf

        ticker = yf.Ticker(yf_symbol)
        prices_df = ticker.history(period=period, actions=False)
        shares_series = self._shares_getter(ticker, yf_symbol)()
        return prices_df, shares_series

    def shares_full(self, yf_symbol: str, start_date: date) -> Any:
        import yfinance as yf

        ticker = yf.Ticker(yf_symbol)
        # get_shares_full accepts start= as an ISO string; end defaults to now.
        return self._shares_getter(ticker, yf_symbol)(start=start_date.isoformat())

    @staticmethod
    def _shares_getter(ticker: Any, yf_symbol: str):
        """`get_shares_full` exists on yfinance >= 0.2.30-ish.

        Falling back to `info.sharesOutstanding` is not an option: that is a
        scalar, and every caller here needs a series. Surface it as a
        capability failure so the fetcher can raise Permanent rather than
        retrying an unfixable call.
        """
        getter = getattr(ticker, "get_shares_full", None)
        if getter is None:
            raise SourceCapabilityError(
                f"{yf_symbol}: get_shares_full not available on this yfinance "
                f"version. Install yfinance >= 0.2.30."
            )
        return getter


DEFAULT_SOURCE: FundamentalsSource = YFinanceFundamentalsSource()
"""The production source. Stateless, so one module-level instance is fine, and
binding it as a default argument keeps the fetchers' signatures honest: the
dependency is visible at the call site instead of hidden in the body."""
