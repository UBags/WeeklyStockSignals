"""yfinance source.

Wraps yf.download() with batching, retry-with-backoff, and validation.
The output is a dict of {nse_symbol: FetchOutcome} so callers can
match results back to their input tickers.

Design notes:
  - We use yf.download(list_of_symbols, ...) for efficiency. yfinance
    handles HTTP keep-alive internally for batched calls.
  - Batch size is small enough (default 50) that one bad symbol
    doesn't poison the whole batch — yfinance returns a partial frame
    with NaN columns for symbols that errored.
  - We re-add `.NS` suffix for the actual yfinance call and strip it
    when constructing the FetchOutcome — the dict is keyed by the
    NSE symbol the caller knows.
  - Daily interval ('1d') by default; '1h' for the intraday path.
    Both produce daily-OR-finer bars that the reconciler will write
    to ohlcv_bars with timeframe='1D' (we aggregate hourly→daily-
    provisional in the reconciler, not here).

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
from datetime import date, datetime, timedelta
from typing import Iterable

from .types import FetchOutcome, RawBar

logger = logging.getLogger(__name__)


def to_yfinance_symbol(nse_symbol: str) -> str:
    """NSE 'RELIANCE' → yfinance 'RELIANCE.NS'.

    Note: yfinance has a hardcoded '.NS' for NSE-listed equities and
    ETFs. The handful of dual-listed names that have both BSE (.BO)
    and NSE (.NS) variants are pulled from .NS only; we don't merge
    cross-exchange.
    """
    return f"{nse_symbol.upper()}.NS"


def from_yfinance_symbol(yf_symbol: str) -> str:
    """'RELIANCE.NS' → 'RELIANCE'. Defensive against unknown suffixes."""
    if "." in yf_symbol:
        return yf_symbol.split(".", 1)[0].upper()
    return yf_symbol.upper()


class YFinanceSource:
    """Stateful for batching, retry counters, etc. Kept thin — the
    interesting business logic lives in the reconciler."""

    def __init__(
        self,
        batch_size: int = 50,
        max_retries: int = 3,
        retry_backoff_base: float = 2.0,
        request_delay_seconds: float = 0.0,
    ):
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base
        self._request_delay = request_delay_seconds

    # ---------- public ---------- #

    def fetch_daily_history(
        self,
        nse_symbols: Iterable[str],
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
        symbols_list = list(nse_symbols)
        for batch in self._batched(symbols_list, self._batch_size):
            batch_results = self._fetch_batch_daily(batch, start, end_date)
            results.update(batch_results)
            if self._request_delay > 0:
                time.sleep(self._request_delay)
        return results

    def fetch_hourly_recent(
        self,
        nse_symbols: Iterable[str],
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
        symbols_list = list(nse_symbols)
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
        nse_symbols: list[str],
        start: date,
        end: date,
    ) -> dict[str, FetchOutcome]:
        import yfinance as yf  # lazy: only required when an ETL run fires

        yf_symbols = [to_yfinance_symbol(s) for s in nse_symbols]
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
                return self._normalize_df_to_outcomes(df, nse_symbols)
            except Exception as e:
                last_error = e
                logger.warning(
                    "yfinance batch failed; will retry",
                    extra={
                        "attempt": attempt,
                        "batch_size": len(nse_symbols),
                        "error": str(e),
                    },
                )
                if attempt < self._max_retries:
                    time.sleep(self._retry_backoff_base ** attempt)

        # All retries exhausted — every ticker in the batch fails.
        err = f"batch fetch failed after {self._max_retries} retries: {last_error}"
        return {
            s: FetchOutcome(yfinance_symbol=to_yfinance_symbol(s), error_message=err)
            for s in nse_symbols
        }

    def _fetch_batch_hourly(
        self,
        nse_symbols: list[str],
        lookback_days: int,
    ) -> dict[str, FetchOutcome]:
        import yfinance as yf

        yf_symbols = [to_yfinance_symbol(s) for s in nse_symbols]
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
                return self._normalize_df_to_outcomes(df, nse_symbols)
            except Exception as e:
                last_error = e
                if attempt < self._max_retries:
                    time.sleep(self._retry_backoff_base ** attempt)

        err = f"hourly batch failed after {self._max_retries} retries: {last_error}"
        return {
            s: FetchOutcome(yfinance_symbol=to_yfinance_symbol(s), error_message=err)
            for s in nse_symbols
        }

    def _normalize_df_to_outcomes(
        self,
        df,
        nse_symbols: list[str],
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
            for s in nse_symbols:
                results[s] = FetchOutcome(
                    yfinance_symbol=to_yfinance_symbol(s),
                    error_message="yfinance returned empty DataFrame",
                )
            return results

        is_multi = len(nse_symbols) > 1

        for nse_symbol in nse_symbols:
            yf_symbol = to_yfinance_symbol(nse_symbol)
            try:
                if is_multi:
                    # Multi-index columns: df[yf_symbol] gives sub-frame
                    if yf_symbol not in df.columns.get_level_values(0):
                        results[nse_symbol] = FetchOutcome(
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
                    l = row.get("Low")
                    c = row.get("Close")
                    ac = row.get("Adj Close", c)
                    v = row.get("Volume", 0.0)
                    # yfinance returns NaN for missing-data rows in
                    # multi-symbol responses. Skip them rather than
                    # writing garbage.
                    if any(_is_nan(x) for x in (o, h, l, c)):
                        continue
                    trading_date = ts.date() if hasattr(ts, "date") else ts
                    bars.append(RawBar(
                        trading_date=trading_date,
                        open=float(o),
                        high=float(h),
                        low=float(l),
                        close=float(c),
                        adj_close=float(ac) if not _is_nan(ac) else float(c),
                        volume=float(v) if not _is_nan(v) else 0.0,
                    ))
                results[nse_symbol] = FetchOutcome(
                    yfinance_symbol=yf_symbol, bars=tuple(bars),
                )
            except Exception as e:
                results[nse_symbol] = FetchOutcome(
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
