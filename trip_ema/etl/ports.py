"""Ports for the outside world that `etl` reads bars from.

`BarSource` is what the ETL passes depend on. `YFinanceSource` is the only
production implementation, and it already satisfies this structurally — the
Protocol is written down so the dependency is a named type rather than a
concrete class the runners happen to import.

`Downloader` is the seam one level below that: the raw call into yfinance.
It exists because `_fetch_batch_daily` used to do `import yfinance as yf` and
call `yf.download(...)` inline, which left the test suite no choice but to
patch `yfinance.download` globally. Injecting the callable makes the batching,
retry, and throttle logic testable without patching anything and without
yfinance installed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from datetime import date
from typing import Any, Protocol, runtime_checkable

from .types import FetchOutcome

Downloader = Callable[..., Any]
"""`(**kwargs) -> DataFrame`. The kwargs are yfinance's own (tickers, start,
end, interval, ...). Deliberately untyped: this is the boundary with a library
whose signature is not ours to pin down."""

Sleeper = Callable[[float], None]
"""`(seconds) -> None`. `time.sleep` in production; a recorder in tests."""


@runtime_checkable
class BarSource(Protocol):
    """What the ETL passes need from a bar provider."""

    @property
    def exchange(self) -> str: ...

    def fetch_daily_history(
        self, symbols: Iterable[str], start: date, end: date | None = None,
    ) -> dict[str, FetchOutcome]: ...

    def fetch_hourly_recent(
        self, symbols: Iterable[str], lookback_days: int = 5,
    ) -> dict[str, FetchOutcome]: ...


def yf_download(**kwargs: Any) -> Any:
    """The production Downloader. yfinance is imported lazily, per call — the
    package is only needed when an ETL run actually fires, and the test suite
    must not require it to be installed."""
    import yfinance as yf

    return yf.download(**kwargs)


_YF_ERRORS_LOCK = threading.Lock()


def snapshot_and_clear_yf_errors(yf_symbols: list[str]) -> dict[str, str]:
    """B1: capture yfinance.shared._ERRORS entries for these symbols under a
    lock, then remove them so subsequent batches on other threads don't see
    stale attributions.

    yfinance records per-symbol failures (IntCastingNaNError, "possibly
    delisted", …) in yfinance.shared._ERRORS as a process-global dict. Under
    concurrent batched downloads, two threads populating that dict would
    cross-contaminate. Snapshot-and-clear per batch under one lock avoids
    the race, at the cost of coupling to a yfinance internal — pinned in D5.

    Lives HERE, not in yfinance_source, because the architecture guardrail
    (Phase 4B closed seam) requires yfinance to be imported ONLY from ports.

    Returns {yf_symbol: error_string} for symbols this batch owned. Symbols
    not in _ERRORS are omitted (they succeeded or had no error surface).
    """
    try:
        import yfinance.shared as _yfs  # deferred: yfinance is heavy
    except Exception:  # noqa: BLE001
        return {}
    captured: dict[str, str] = {}
    with _YF_ERRORS_LOCK:
        errs = getattr(_yfs, "_ERRORS", None)
        if not isinstance(errs, dict):
            return {}
        for s in yf_symbols:
            if s in errs:
                captured[s] = str(errs.pop(s))
    return captured
