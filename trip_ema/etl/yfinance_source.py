"""yfinance source.

Wraps yf.download() with batching, retry-with-backoff, and validation.
The output is a dict of {bare_symbol: FetchOutcome} so callers can
match results back to their input tickers.

Design notes:
  - We use yf.download(list_of_symbols, ...) for efficiency. yfinance
    handles HTTP keep-alive internally for batched calls.
  - Batch size is small enough (default 50) that one bad symbol
    doesn't poison the whole batch — yfinance returns a partial frame
    with NaN columns for symbols that errored.
  - The exchange-specific suffix (NSE → '.NS', BSE → '.BO', NYSE →
    '', etc.) is applied via `yfinance_symbol(symbol, exchange)` from
    the `exchanges` module. The output dict is keyed by the bare
    symbol the caller knows.
  - Daily interval ('1d') by default; '1h' for the intraday path.
    Both produce daily-OR-finer bars that the reconciler will write
    to ohlcv_bars with timeframe='1D' (we aggregate hourly→daily-
    provisional in the reconciler, not here).

Phase 14.6a: the `YFinanceSource` now carries an `exchange` so it
can be constructed once per ETL run and used across all batches.
The legacy module-level `to_yfinance_symbol(symbol)` (NSE-hardcoded)
function is retained as a thin deprecation shim — it forwards to
the canonical mapper with exchange='NSE'. Internal call sites use
the new `exchanges.yfinance_symbol(symbol, exchange)` directly via
`self._yf(symbol)` so each fetch is exchange-correct.

NOT in here:
  - Reconciliation with existing DB state (that's reconciler.py).
  - Concurrency across batches (that's runner.py — uses
    ThreadPoolExecutor over batches).
  - The 730-day intraday-history limit — we just respect it by
    capping the requested period.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from collections.abc import Iterable

from .exchanges import (
    UnsupportedExchangeError,
    from_yfinance_symbol as _from_yf_canonical,
    yfinance_symbol as _to_yf_canonical,
)
from .types import FetchOutcome, RawBar

logger = logging.getLogger(__name__)


# ---------- Module-level mappers (legacy entry points) ---------- #


def to_yfinance_symbol(symbol: str, exchange: str = "NSE") -> str:
    """Format a bare symbol for yfinance.

    Default exchange is 'NSE' for backward-compatibility with the
    legacy single-exchange callers. New code should pass `exchange`
    explicitly. Internal callers in this module use the canonical
    `exchanges.yfinance_symbol` directly through `YFinanceSource._yf`,
    so this function exists primarily for external imports.
    """
    return _to_yf_canonical(symbol, exchange)


def from_yfinance_symbol(yf_symbol: str, exchange: str = "NSE") -> str:
    """Inverse of `to_yfinance_symbol`. Defaults to NSE for
    backward-compatibility (same reasoning as `to_yfinance_symbol`)."""
    try:
        return _from_yf_canonical(yf_symbol, exchange)
    except UnsupportedExchangeError:
        # Fall back to the legacy "strip after first dot" behaviour
        # when called with an unknown exchange. Callers that care
        # about strict validation should use exchanges.from_yfinance_symbol
        # directly.
        if "." in yf_symbol:
            return yf_symbol.split(".", 1)[0].upper()
        return yf_symbol.upper()


# ---------- Source class ---------- #


class YFinanceSource:
    """Stateful for batching, retry counters, etc. Kept thin — the
    interesting business logic lives in the reconciler.

    Phase 14.6a: each instance is bound to a single exchange. The
    runner constructs one source per ETL pass; the exchange is
    inherited from the pass's exchange kwarg. All symbol formatting
    within this instance goes through `self._yf(symbol)`.
    """

    def __init__(
        self,
        exchange: str = "NSE",
        batch_size: int = 50,
        max_retries: int = 3,
        retry_backoff_base: float = 2.0,
        request_delay_seconds: float = 0.0,
    ):
        # NB: exchange is validated by the runner before YFinanceSource
        # is constructed. We trust it here. A misformatted exchange
        # would surface when _yf() is first called.
        self._exchange = exchange
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base
        self._request_delay = request_delay_seconds

    @property
    def exchange(self) -> str:
        return self._exchange

    # ---------- internal symbol formatter ---------- #

    def _yf(self, symbol: str) -> str:
        """This instance's exchange-aware symbol mapper. All callsites
        within YFinanceSource go through here; external callers use
        the module-level `to_yfinance_symbol`."""
        return _to_yf_canonical(symbol, self._exchange)

    # ---------- public ---------- #

    def fetch_daily_history(
        self,
        symbols: Iterable[str],
        start: date,
        end: date | None = None,
    ) -> dict[str, FetchOutcome]:
        """Fetch daily OHLCV+adjclose for each symbol over [start, end].

        end defaults to today. Returns one FetchOutcome per requested
        symbol, regardless of success — a failed symbol gets a
        FetchOutcome with empty bars and error_message set.
        """
        end_date = end or date.today()
        results: dict[str, FetchOutcome] = {}
        symbols_list = list(symbols)
        for batch in self._batched(symbols_list, self._batch_size):
            batch_results = self._fetch_batch_daily(batch, start, end_date)
            results.update(batch_results)
            if self._request_delay > 0:
                time.sleep(self._request_delay)
        return results

    def fetch_hourly_recent(
        self,
        symbols: Iterable[str],
        lookback_days: int = 5,
    ) -> dict[str, FetchOutcome]:
        """Fetch hourly bars for the last `lookback_days`. Used by the
        intraday Tier 1 path; we then aggregate to daily-provisional
        in the reconciler.

        yfinance has a 730-day retention limit for intraday data, so
        lookback_days is capped at 730. Default 5 is enough to be
        defensive against missed runs.
        """
        results: dict[str, FetchOutcome] = {}
        capped_lookback = min(lookback_days, 730)
        symbols_list = list(symbols)
        for batch in self._batched(symbols_list, self._batch_size):
            batch_results = self._fetch_batch_hourly(batch, capped_lookback)
            results.update(batch_results)
            if self._request_delay > 0:
                time.sleep(self._request_delay)
        return results

    # ---------- internal ---------- #

    @staticmethod
    def _batched(items: list, n: int):
        for i in range(0, len(items), n):
            yield items[i : i + n]

    def _fetch_batch_daily(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> dict[str, FetchOutcome]:
        import yfinance as yf  # lazy: only required when an ETL run fires

        yf_symbols = [self._yf(s) for s in symbols]
        last_error = None
        for attempt in range(1, self._max_retries + 1):
            try:
                df = yf.download(
                    tickers=yf_symbols,
                    start=start.isoformat(),
                    end=(end + timedelta(days=1)).isoformat(),  # yf is end-exclusive
                    interval="1d",
                    auto_adjust=False,
                    actions=False,
                    threads=False,
                    progress=False,
                    group_by="ticker",
                )
                return self._normalize_df_to_outcomes(df, symbols)
            except Exception as e:
                last_error = e
                logger.warning(
                    "yfinance batch failed; will retry",
                    extra={
                        "attempt": attempt,
                        "batch_size": len(symbols),
                        "exchange": self._exchange,
                        "error": str(e),
                    },
                )
                if attempt < self._max_retries:
                    time.sleep(self._retry_backoff_base ** attempt)

        # All retries exhausted — every ticker in the batch fails.
        err = f"batch fetch failed after {self._max_retries} retries: {last_error}"
        return {
            s: FetchOutcome(yfinance_symbol=self._yf(s), error_message=err)
            for s in symbols
        }

    def _fetch_batch_hourly(
        self,
        symbols: list[str],
        lookback_days: int,
    ) -> dict[str, FetchOutcome]:
        import yfinance as yf

        yf_symbols = [self._yf(s) for s in symbols]
        last_error = None
        for attempt in range(1, self._max_retries + 1):
            try:
                df = yf.download(
                    tickers=yf_symbols,
                    period=f"{lookback_days}d",
                    interval="1h",
                    auto_adjust=False,
                    actions=False,
                    threads=False,
                    progress=False,
                    group_by="ticker",
                )
                return self._normalize_df_to_outcomes(df, symbols)
            except Exception as e:
                last_error = e
                if attempt < self._max_retries:
                    time.sleep(self._retry_backoff_base ** attempt)

        err = f"hourly batch failed after {self._max_retries} retries: {last_error}"
        return {
            s: FetchOutcome(yfinance_symbol=self._yf(s), error_message=err)
            for s in symbols
        }

    def _normalize_df_to_outcomes(
        self,
        df,
        symbols: list[str],
    ) -> dict[str, FetchOutcome]:
        """yfinance returns either:
          - a multi-index DataFrame {ticker: {col: series}} for multi-symbol calls
          - a flat single-symbol DataFrame for one-symbol calls
        We normalize to per-ticker FetchOutcome regardless.

        A symbol that yfinance couldn't resolve gets a FetchOutcome
        with empty bars and an error_message; this is distinct from a
        symbol that succeeded but has zero bars in the period (also
        empty bars, error_message=None).
        """
        results: dict[str, FetchOutcome] = {}

        if df is None or df.empty:
            for s in symbols:
                results[s] = FetchOutcome(
                    yfinance_symbol=self._yf(s),
                    error_message="yfinance returned empty DataFrame",
                )
            return results

        is_multi = len(symbols) > 1

        for symbol in symbols:
            yf_symbol = self._yf(symbol)
            try:
                if is_multi:
                    # Multi-index columns: df[yf_symbol] gives sub-frame
                    if yf_symbol not in df.columns.get_level_values(0):
                        results[symbol] = FetchOutcome(
                            yfinance_symbol=yf_symbol,
                            error_message="symbol not in returned columns",
                        )
                        continue
                    sub = df[yf_symbol]
                else:
                    sub = df

                bars: list[RawBar] = []
                for ts, row in sub.iterrows():
                    o = row.get("Open")
                    h = row.get("High")
                    lo = row.get("Low")
                    c = row.get("Close")
                    ac = row.get("Adj Close", c)
                    v = row.get("Volume", 0.0)
                    # yfinance returns NaN for missing-data rows in
                    # multi-symbol responses. Skip them rather than
                    # writing garbage.
                    if any(_is_nan(x) for x in (o, h, lo, c)):
                        continue
                    trading_date = ts.date() if hasattr(ts, "date") else ts
                    bars.append(RawBar(
                        trading_date=trading_date,
                        open=float(o),
                        high=float(h),
                        low=float(lo),
                        close=float(c),
                        adj_close=float(ac) if not _is_nan(ac) else float(c),
                        volume=float(v) if not _is_nan(v) else 0.0,
                    ))
                results[symbol] = FetchOutcome(
                    yfinance_symbol=yf_symbol, bars=tuple(bars),
                )
            except Exception as e:
                results[symbol] = FetchOutcome(
                    yfinance_symbol=yf_symbol,
                    error_message=f"normalization failed: {e}",
                )

        return results


def _is_nan(v) -> bool:
    """NaN-safe check; works for pandas NaN, numpy NaN, and float('nan')."""
    if v is None:
        return True
    try:
        return v != v  # noqa: PLR0124 — the canonical NaN test
    except Exception:
        return False
