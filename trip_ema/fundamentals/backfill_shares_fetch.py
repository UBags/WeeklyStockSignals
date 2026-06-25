"""yfinance historical shares-outstanding fetcher (backfill).

Wraps `Ticker.get_shares_full(start=...)` which returns a pandas
Series indexed by date with shares-outstanding values.

Unlike `shares_fetch.py` (which calls `Ticker.info` for a SINGLE
current scalar), this module returns a HISTORICAL TIME SERIES — one
shares-outstanding snapshot per filing date over `[start, end]`.
That's exactly what `instrument_shares` is designed to hold: its
composite PK on `(ticker_id, as_of_date)` lets us store many
snapshots per ticker.

Once `instrument_shares` is populated with historical snapshots,
`compute-market-cap` (which already does `get_as_of(tid, trade_date)`
asof-lookups) automatically picks the correct shares figure for each
historical trade_date, and produces COMPUTED rows (not APPROX) for
the full backfill range.

Typed exceptions match the pattern in shares_fetch.py: rate-limit,
transient, permanent. The runner above this module handles the
retry/cool-off policy at the loop level.

What this module does:
  - fetch_historical_shares(symbol, exchange, start_date)
    → list[HistoricalSharesSnapshot]

What it deliberately does NOT do:
  - DB writes. The runner does that.
  - Sleep between calls. The runner does that.
  - Combine with prices to produce mcap. That's market_cap_fetch.py's
    job. We only return shares; the date is the filing date that
    yfinance reports, not a trade date.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from ..etl.exchanges import yfinance_symbol

logger = logging.getLogger(__name__)


# ---------- Public dataclass ---------- #


@dataclass(frozen=True, slots=True)
class HistoricalSharesSnapshot:
    """One historical filing snapshot of shares-outstanding for a
    ticker. Ready for insert into instrument_shares with
    shares_source='YFINANCE'.

    `as_of_date` is the date yfinance reports the snapshot for —
    typically a quarterly filing date or split adjustment date.
    Multiple snapshots per ticker are normal; that's the whole
    point of using `get_shares_full()` over `.info['sharesOutstanding']`.
    """
    as_of_date: date
    shares_outstanding: int


# ---------- Typed exceptions ---------- #


class HistoricalSharesFetchError(Exception):
    """Base class. Runner catches this to count a ticker as failed."""


class HistoricalSharesFetchRateLimited(HistoricalSharesFetchError):
    """yfinance signaled rate limit. Runner applies longer cool-off."""


class HistoricalSharesFetchTransient(HistoricalSharesFetchError):
    """Network/HTTP transient failure. Retryable."""


class HistoricalSharesFetchPermanent(HistoricalSharesFetchError):
    """Ticker doesn't exist, OR yfinance returned no usable shares
    history. Runner won't retry."""


# ---------- The fetch function ---------- #


def fetch_historical_shares(
    symbol: str,
    exchange: str,
    start_date: date,
) -> list[HistoricalSharesSnapshot]:
    """Fetch historical shares-outstanding snapshots for ONE ticker
    from `start_date` to today.

    Returns the list of snapshots in date order (oldest first), with
    duplicates by date removed (yfinance occasionally emits multiple
    entries on the same date; we keep the last/largest). Empty list
    is OK — means yfinance has no shares history beyond what's
    already in `Ticker.info`, which is rare but possible for newly
    listed tickers.

    Raises HistoricalSharesFetch* on the corresponding failure modes,
    same semantics as shares_fetch.py / market_cap_fetch.py.
    """
    yf_symbol = yfinance_symbol(symbol, exchange)

    try:
        shares_series = _fetch_yfinance_shares_full(yf_symbol, start_date)
    except (
        HistoricalSharesFetchRateLimited,
        HistoricalSharesFetchTransient,
        HistoricalSharesFetchPermanent,
    ):
        raise
    except Exception as e:
        raise _classify_yfinance_exception(yf_symbol, e) from e

    if shares_series is None or len(shares_series) == 0:
        raise HistoricalSharesFetchPermanent(
            f"{yf_symbol}: get_shares_full returned empty"
        )

    # Build (date, shares) pairs, filter to range, dedupe by date.
    pairs: dict[date, int] = {}
    for ts, value in zip(shares_series.index, shares_series.values):
        d = _to_date(ts)
        v = _to_positive_int(value)
        if d is None or v is None:
            continue
        if d < start_date:
            continue
        # Last-write-wins on duplicate dates. yfinance sometimes
        # emits intra-day duplicates with slightly different counts;
        # the last value in the series is typically the canonical
        # one for that filing day.
        pairs[d] = v

    if not pairs:
        raise HistoricalSharesFetchPermanent(
            f"{yf_symbol}: get_shares_full had no parseable rows in "
            f"[{start_date}, today]"
        )

    snapshots = [
        HistoricalSharesSnapshot(as_of_date=d, shares_outstanding=v)
        for d, v in sorted(pairs.items())
    ]
    return snapshots


# ---------- Internals ---------- #


def _classify_yfinance_exception(
    yf_symbol: str, e: Exception,
) -> HistoricalSharesFetchError:
    """Map raw exception → typed subtype. Same string-match pattern
    as the other Phase 2-4A fetchers."""
    msg = str(e).lower()
    if "rate" in msg and "limit" in msg:
        return HistoricalSharesFetchRateLimited(f"{yf_symbol}: {e}")
    if any(t in msg for t in (
        "timeout", "timed out", "connection reset",
        "connection refused", "remote end closed",
    )):
        return HistoricalSharesFetchTransient(f"{yf_symbol}: {e}")
    if any(t in msg for t in (
        "delisted", "no data", "symbol may be",
        "not found", "404",
    )):
        return HistoricalSharesFetchPermanent(f"{yf_symbol}: {e}")
    # Unknown — treat as transient so we don't permanently park
    # tickers on novel yfinance errors.
    return HistoricalSharesFetchTransient(f"{yf_symbol}: {e}")


def _fetch_yfinance_shares_full(yf_symbol: str, start_date: date):
    """The actual yfinance call. Returns a pandas Series (or None).

    Isolated for test patchability — tests replace this with a stub
    returning a synthetic Series.
    """
    import yfinance as yf

    ticker = yf.Ticker(yf_symbol)
    getter = getattr(ticker, "get_shares_full", None)
    if getter is None:
        raise HistoricalSharesFetchPermanent(
            f"{yf_symbol}: get_shares_full not available on this yfinance "
            f"version. Install yfinance >= 0.2.30."
        )
    # get_shares_full() accepts start= and end= as ISO strings or
    # date/datetime objects. We pass start; end defaults to now.
    return getter(start=start_date.isoformat())


def _to_date(ts) -> date | None:
    """Convert pandas Timestamp / datetime / string → date.
    Returns None on parse failure."""
    if hasattr(ts, "date"):
        try:
            return ts.date()
        except Exception:
            return None
    if isinstance(ts, date):
        return ts
    try:
        return datetime.strptime(str(ts)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _to_positive_int(v) -> int | None:
    """Coerce numpy/pandas number → positive int, else None.

    Zero treated as missing (a real ticker with zero shares
    doesn't exist; yfinance has been seen to return 0 for delisted
    rows where the right answer is NULL)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    if f <= 0:
        return None
    return int(f)