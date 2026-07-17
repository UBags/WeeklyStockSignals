"""yfinance source.

Wraps a Downloader (see ports.py) with batching, retry-with-backoff, throttle
detection, and validation. The output is a dict of {bare_symbol: FetchOutcome}
so callers can match results back to their input tickers.

Collaborators are injected, not imported: the raw download call, the sleeper,
and the ThrottleGate. The gate in particular is meant to be SHARED across every
source in a fetch pass — a single solo re-fetch cannot tell a delisted ticker
from a rate-limit, but three in a row can, and only a shared object can see
that. See throttle.py.

Design notes:
  - We download a list of symbols at once for efficiency. yfinance
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
from datetime import UTC, date, datetime, timedelta
from collections.abc import Iterable

from .exchanges import (
    UnsupportedExchangeError,
    from_yfinance_symbol as _from_yf_canonical,
    yfinance_symbol as _to_yf_canonical,
)
from .ports import (
    Downloader,
    Sleeper,
    snapshot_and_clear_yf_errors,
    yf_download,
)
from .throttle import ThrottleConfig, ThrottleGate, looks_rate_limited
from .types import FetchOutcome, RawBar

logger = logging.getLogger(__name__)


# ---------- yfinance internal error surface (B1) ---------- #
#
# `snapshot_and_clear_yf_errors` lives in .ports — this module cannot import
# yfinance directly (Phase 4B closed seam). We import the port at the top of
# this file and call it inline in the batched fetchers below.


def _classify_empty(yf_symbol: str, errors: dict[str, str]) -> str:
    """B1: given a symbol that returned no bars, decide whether the cause
    was IntCastingNaN (recoverable under repair=False), a delisting notice
    (genuinely dataless), or an unclassified empty."""
    msg = errors.get(yf_symbol, "")
    if "IntCastingNaN" in msg:
        return "intcast"
    if "delisted" in msg.lower():
        return "delisted"
    return "empty"


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
        repair: bool = True,
        throttle_backoff_seconds: float = 30.0,
        download: Downloader = yf_download,
        sleep: Sleeper = time.sleep,
        throttle: ThrottleGate | None = None,
    ):
        """`download`, `sleep` and `throttle` are collaborators, injected.

        `download` is the raw call into the provider. It used to be an inline
        lazy import of the yfinance module followed by a call to its download
        function, which left tests no choice but to patch that function
        globally.

        `throttle` is the rate-limit state. Pass ONE gate to every source in
        a fetch pass and they corroborate each other's empty frames — which
        is the whole point: a solo re-fetch cannot detect a throttle on its
        own, but three of them in a row can. Defaults to a private gate,
        which is correct for a source used alone.
        """
        # NB: exchange is validated by the runner before YFinanceSource
        # is constructed. We trust it here. A misformatted exchange
        # would surface when _yf() is first called.
        self._exchange = exchange
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base
        self._request_delay = request_delay_seconds
        # repair=True fixes split-residue / 100x bad ticks for the bulk
        # fetch, but crashes (IntCastingNaNError) on some tickers' NaN
        # volumes. The solo-retry recovery pass sets repair=False to
        # rescue those.
        self._repair = repair
        self._download = download
        self._sleep = sleep
        # Yahoo's rate-limit is NOT an exception — see throttle.py.
        self._throttle_backoff = throttle_backoff_seconds
        self._gate = throttle or ThrottleGate(
            ThrottleConfig(base_seconds=throttle_backoff_seconds),
            sleep=sleep,
        )

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
        # The RUNNER always passes an explicit, exchange-aware `end`. This
        # fallback exists only for ad-hoc/direct use and is never on the signal
        # path. UTC, not date.today(): deterministic rather than machine-local.
        end_date = end or datetime.now(UTC).date()
        results: dict[str, FetchOutcome] = {}
        symbols_list = list(symbols)
        for batch in self._batched(symbols_list, self._batch_size):
            batch_results = self._fetch_batch_daily(batch, start, end_date)
            results.update(batch_results)
            if self._request_delay > 0:
                self._sleep(self._request_delay)
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
                self._sleep(self._request_delay)
        return results

    # ---------- internal ---------- #

    @staticmethod
    def _batched(items: list, n: int):
        for i in range(0, len(items), n):
            yield items[i : i + n]

    @staticmethod
    def _all_empty(
        outcomes: dict[str, FetchOutcome], symbols: list[str],
    ) -> bool:
        """True when every symbol in the frame came back with no bars and no
        error — the shape a throttled response takes.

        Yahoo does not raise when it throttles. It returns the frame with every
        requested symbol's column present and all-NaN, which
        _normalize_df_to_outcomes faithfully turns into
        FetchOutcome(bars=(), error_message=None) — a "success" with nothing in
        it.

        This function no longer decides whether that IS a throttle; it only
        reports the shape. The decision needs corroboration across fetches, not
        just across columns, and so it lives on the ThrottleGate — which is what
        finally lets the solo (batch-of-one) recovery path detect a rate-limit
        it previously could not see. See throttle.py.
        """
        if not symbols:
            return False
        return all(
            (o := outcomes.get(s)) is not None
            and o.succeeded
            and not o.bars
            for s in symbols
        )

    def _fetch_batch_daily(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> dict[str, FetchOutcome]:
        yf_symbols = [self._yf(s) for s in symbols]
        last_error = None
        for attempt in range(1, self._max_retries + 1):
            # Sit out any backoff window another fetch in this pass opened.
            # This is what slows the WHOLE pass down once Yahoo starts
            # throttling, instead of each fetch discovering it alone.
            self._gate.wait()
            try:
                df = self._download(
                    tickers=yf_symbols,
                    start=start.isoformat(),
                    end=(end + timedelta(days=1)).isoformat(),  # yf is end-exclusive
                    interval="1d",
                    auto_adjust=False,  # split-adjusted only; dividends parked in adj_close (§3.2)
                    actions=False,
                    repair=self._repair,  # False on the solo-retry pass
                    threads=False,
                    progress=False,
                    group_by="ticker",
                )
                # B1: snapshot yfinance's own per-symbol error surface for this
                # batch, before any other batch on another thread can pollute
                # or clear it. Passed to _normalize so empty outcomes can be
                # classified intcast / delisted / empty.
                errors = snapshot_and_clear_yf_errors(yf_symbols)
                outcomes = self._normalize_df_to_outcomes(
                    df, symbols, errors=errors,
                )

                # The throttle check lives HERE, after normalisation, because the
                # rate-limit is invisible at the exception level — it is a shape
                # in the returned data, not an error. The GATE decides whether
                # this empty frame is evidence of a throttle, because that
                # decision needs the rest of the pass to make it: one empty solo
                # frame is ambiguous, three in a row are not.
                if self._all_empty(outcomes, symbols):
                    if (
                        attempt < self._max_retries
                        and self._gate.observe_empty(len(symbols))
                    ):
                        continue
                else:
                    self._gate.observe_data()

                return outcomes
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
                if attempt >= self._max_retries:
                    continue
                if looks_rate_limited(e):
                    # An explicit 429. This is not a flake; retrying in two
                    # seconds is another swing at the same closed door. Take the
                    # long backoff, and let the rest of the pass see it.
                    self._gate.observe_rate_limit_error(e)
                    continue
                self._sleep(self._retry_backoff_base ** attempt)

        # All retries exhausted — every ticker in the batch fails.
        err = f"batch fetch failed after {self._max_retries} retries: {last_error}"
        return {
            s: FetchOutcome(
                yfinance_symbol=self._yf(s),
                error_message=err,
                failure_kind="error",
            )
            for s in symbols
        }

    def _fetch_batch_hourly(
        self,
        symbols: list[str],
        lookback_days: int,
    ) -> dict[str, FetchOutcome]:
        yf_symbols = [self._yf(s) for s in symbols]
        last_error = None
        for attempt in range(1, self._max_retries + 1):
            self._gate.wait()
            try:
                df = self._download(
                    tickers=yf_symbols,
                    period=f"{lookback_days}d",
                    interval="1h",
                    auto_adjust=False,  # split-adjusted only; dividends parked in adj_close (§3.2)
                    actions=False,
                    repair=self._repair,  # False on the solo-retry pass
                    threads=False,
                    progress=False,
                    group_by="ticker",
                )
                # B1: see _fetch_batch_daily for rationale.
                errors = snapshot_and_clear_yf_errors(yf_symbols)
                outcomes = self._normalize_df_to_outcomes(
                    df, symbols, errors=errors,
                )
                if self._all_empty(outcomes, symbols):
                    if (
                        attempt < self._max_retries
                        and self._gate.observe_empty(len(symbols))
                    ):
                        continue
                else:
                    self._gate.observe_data()
                return outcomes
            except Exception as e:
                last_error = e
                if attempt >= self._max_retries:
                    continue
                if looks_rate_limited(e):
                    self._gate.observe_rate_limit_error(e)
                    continue
                self._sleep(self._retry_backoff_base ** attempt)

        err = f"hourly batch failed after {self._max_retries} retries: {last_error}"
        return {
            s: FetchOutcome(
                yfinance_symbol=self._yf(s),
                error_message=err,
                failure_kind="error",
            )
            for s in symbols
        }

    @staticmethod
    def _subframe_for_symbol(df, yf_symbol: str):
        """Return a per-ticker sub-DataFrame with FLAT single-level
        columns (Open/High/Low/Close/Volume/[Adj Close]), tolerant of
        every column shape yfinance emits:
          - flat single-level columns (legacy single-symbol)
          - MultiIndex (Ticker, Field)  -> df[yf_symbol]
          - MultiIndex (Field, Ticker)  -> df.xs(symbol, axis=1, level=1)
        Returns None if the symbol isn't present in a multi-symbol frame.
        """
        cols = df.columns
        if getattr(cols, "nlevels", 1) == 1:
            # Flat columns already — single-symbol legacy shape.
            return df
        # MultiIndex: find which level carries the ticker.
        lvl0 = set(cols.get_level_values(0))
        lvl1 = set(cols.get_level_values(1))
        if yf_symbol in lvl0:
            sub = df[yf_symbol]           # (Ticker, Field) order
        elif yf_symbol in lvl1:
            sub = df.xs(yf_symbol, axis=1, level=1)  # (Field, Ticker) order
        else:
            return None
        # sub may still have a residual level if the frame was 3-level;
        # collapse any remaining MultiIndex to its last level (the field).
        if getattr(sub.columns, "nlevels", 1) > 1:
            sub = sub.copy()
            sub.columns = sub.columns.get_level_values(-1)
        return sub

    def _normalize_df_to_outcomes(
        self,
        df,
        symbols: list[str],
        *,
        errors: dict[str, str] | None = None,
    ) -> dict[str, FetchOutcome]:
        """yfinance returns either:
          - a multi-index DataFrame {ticker: {col: series}} for multi-symbol calls
          - a flat single-symbol DataFrame for one-symbol calls
        We normalize to per-ticker FetchOutcome regardless.

        A symbol that yfinance couldn't resolve gets a FetchOutcome
        with empty bars and an error_message; this is distinct from a
        symbol that succeeded but has zero bars in the period (also
        empty bars, error_message=None).

        B1: `errors` is the snapshot of yfinance.shared._ERRORS for this
        batch's yf_symbols (see _snapshot_and_clear_yf_errors). It lets us
        tag empty outcomes as intcast / delisted / empty so the retry layer
        can route IntCasting straight to repair=False.
        """
        results: dict[str, FetchOutcome] = {}
        errors = errors or {}

        if df is None or df.empty:
            for s in symbols:
                results[s] = FetchOutcome(
                    yfinance_symbol=self._yf(s),
                    error_message="yfinance returned empty DataFrame",
                    failure_kind="error",
                )
            return results

        for symbol in symbols:
            yf_symbol = self._yf(symbol)
            try:
                sub = self._subframe_for_symbol(df, yf_symbol)
                if sub is None:
                    results[symbol] = FetchOutcome(
                        yfinance_symbol=yf_symbol,
                        error_message="symbol not in returned columns",
                        failure_kind="error",
                    )
                    continue

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
                # B1: bars present -> ok. Bars empty -> classify from the
                # yfinance error surface. See _classify_empty.
                kind = "ok" if bars else _classify_empty(yf_symbol, errors)
                results[symbol] = FetchOutcome(
                    yfinance_symbol=yf_symbol,
                    bars=tuple(bars),
                    failure_kind=kind,
                )
            except Exception as e:
                results[symbol] = FetchOutcome(
                    yfinance_symbol=yf_symbol,
                    error_message=f"normalization failed: {e}",
                    failure_kind="error",
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
