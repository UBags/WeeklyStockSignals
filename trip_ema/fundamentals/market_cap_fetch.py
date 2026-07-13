"""yfinance historical market cap fetcher (Phase 4A).

Returns per-(trade_date → (close, shares)) pairs for one ticker over
a date range. The runner combines these into daily_market_cap rows
with source=COMPUTED.

Why this exists separately from market_cap_compute.py:

  market_cap_compute.py joins LOCAL data (ohlcv_bars × 
  instrument_shares). It's used for trade_dates where we have both
  local bars and local shares — i.e. forward-looking from when
  Phase 2 went live.

  This module fetches from yfinance directly. It's used for trade_
  dates older than our local data range. Per the probe results
  (scripts/probe_yfinance_hist_mcap.py, run 2026-05-22), yfinance's
  Ticker.get_shares_full() exposes a historical shares-outstanding
  series going back ~16 months for Indian tickers across all cap
  tiers; Ticker.history() gives 1+ year of daily closes. Joining
  them per-date yields COMPUTED-quality data even for backfill.

  Notably: we do NOT use a separate "YFINANCE_HIST_MCAP" source
  even though the schema reserves the value. yfinance's pre-baked
  marketCap is just close × shares per the probe (ratio
  computed/info = 0.9999-1.0000 across all 5 samples). Since we
  can compute it ourselves with the same inputs, all backfill
  rows get source=COMPUTED. The YFINANCE_HIST_MCAP source value
  remains in the schema for forward compatibility (e.g. if a
  future provider gives us pre-computed mcap that doesn't trivially
  factor into close × shares).

Same typed-exception pattern as industry_fetch / shares_fetch /
financials_fetch: rate-limit, transient, permanent.

What this module does:
  - fetch_historical_mcap_for_ticker(symbol, exchange, from_date,
    to_date) → list[HistoricalMcapPoint]

What it deliberately does NOT do:
  - DB writes. The runner does that.
  - Date-range chunking. Calls Ticker.history(period='1y') or
    longer, which is one yfinance HTTP. If the operator asks for
    > 1 year we need a different approach — flagged for Phase 4C.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from ..etl.exchanges import yfinance_symbol
from .ports import DEFAULT_SOURCE, FundamentalsSource, SourceCapabilityError
from ..domain.market_session import market_today

logger = logging.getLogger(__name__)


# ---------- public types ---------- #


@dataclass(frozen=True, slots=True)
class HistoricalMcapPoint:
    """One (date, close, shares) tuple, ready for upsert into
    daily_market_cap with source=COMPUTED.

    All fields are non-None: if either close or shares is missing
    for a date, that date doesn't get a HistoricalMcapPoint (the
    fetcher filters it out)."""
    trade_date: date
    close_price: float
    shares_outstanding: int


# ---------- typed exceptions ---------- #


class HistoricalMcapFetchError(Exception):
    """Base class. Runner catches this to count a ticker as failed."""


class HistoricalMcapFetchRateLimited(HistoricalMcapFetchError):
    """yfinance signaled rate limit. Runner applies longer cool-off."""


class HistoricalMcapFetchTransient(HistoricalMcapFetchError):
    """Network/HTTP transient failure. Retryable."""


class HistoricalMcapFetchPermanent(HistoricalMcapFetchError):
    """Ticker doesn't exist, OR yfinance returned no usable
    historical data for the date range. Runner won't retry."""


# ---------- the fetch function ---------- #


def fetch_historical_mcap_for_ticker(
    symbol: str,
    exchange: str,
    from_date: date,
    to_date: date,
    *,
    source: FundamentalsSource = DEFAULT_SOURCE,
) -> list[HistoricalMcapPoint]:
    """Fetch historical (close × shares) points for ONE ticker over
    the date range [from_date, to_date].

    Strategy (per the 2026-05-22 probe):

      1. yfinance.Ticker.history(period=...) → DataFrame indexed
         by date, with 'Close' column.
      2. yfinance.Ticker.get_shares_full() → Series indexed by
         date, values = shares outstanding.
      3. For each trade_date in the requested range that appears
         in (1), look up the asof-or-earlier shares value from
         (2) and emit a HistoricalMcapPoint.
      4. Dates in (1) without any asof shares from (2) are dropped
         (we don't emit APPROX rows from this path — only forward
         compute does).

    Returns the list of points in date order (oldest first), which
    is the natural order for backfill writes. Empty list is OK and
    means "no usable data in range."
    """
    yf_symbol = yfinance_symbol(symbol, exchange)

    # Decide what period= to ask yfinance for. The probe showed
    # period='1y' returns ~250 rows for NSE tickers; we map our
    # requested range to a period string yfinance understands.
    today = market_today(exchange)
    days_back = (today - from_date).days
    if days_back <= 35:
        period = "1mo"
    elif days_back <= 95:
        period = "3mo"
    elif days_back <= 200:
        period = "6mo"
    elif days_back <= 380:
        period = "1y"
    elif days_back <= 760:
        period = "2y"
    elif days_back <= 1900:
        period = "5y"
    else:
        period = "max"

    try:
        prices_df, shares_series = source.price_and_shares_history(
            yf_symbol, period=period,
        )
    except (
        HistoricalMcapFetchRateLimited,
        HistoricalMcapFetchTransient,
        HistoricalMcapFetchPermanent,
    ):
        raise
    except SourceCapabilityError as e:
        # The source cannot serve this call on this yfinance version.
        # Not transient: retrying will never fix it, and the message-
        # substring classifier below would wrongly call it Transient.
        raise HistoricalMcapFetchPermanent(str(e)) from e
    except Exception as e:
        raise _classify_yfinance_exception(yf_symbol, e) from e

    if prices_df is None or _is_empty(prices_df):
        raise HistoricalMcapFetchPermanent(
            f"{yf_symbol}: yfinance returned no price history"
        )
    if shares_series is None or len(shares_series) == 0:
        raise HistoricalMcapFetchPermanent(
            f"{yf_symbol}: get_shares_full returned empty"
        )

    # Build a list of (date, shares) pairs in ascending date order
    # so the asof lookup below can walk forward in O(n+m).
    shares_pairs: list[tuple[date, int]] = []
    for ts, value in zip(shares_series.index, shares_series.values, strict=True):
        d = _to_date(ts)
        v = _to_positive_int(value)
        if d is None or v is None:
            continue
        shares_pairs.append((d, v))
    shares_pairs.sort(key=lambda p: p[0])
    if not shares_pairs:
        raise HistoricalMcapFetchPermanent(
            f"{yf_symbol}: get_shares_full had no parseable rows"
        )

    # Walk price dates in ascending order, maintaining a cursor
    # into shares_pairs for asof lookup. This is the standard
    # merge-on-sorted technique — O(n+m) rather than O(n*m).
    price_pairs: list[tuple[date, float]] = []
    for ts in prices_df.index:
        d = _to_date(ts)
        if d is None:
            continue
        if d < from_date or d > to_date:
            continue
        close = prices_df.loc[ts, "Close"]
        c = _coerce_positive_float(close)
        if c is None:
            continue
        price_pairs.append((d, c))
    price_pairs.sort(key=lambda p: p[0])

    points: list[HistoricalMcapPoint] = []
    cursor = 0  # index into shares_pairs
    asof_shares: int | None = None
    for trade_date, close_price in price_pairs:
        # Advance the cursor while the next shares-pair's date is
        # still <= trade_date.
        while cursor < len(shares_pairs) and shares_pairs[cursor][0] <= trade_date:
            asof_shares = shares_pairs[cursor][1]
            cursor += 1
        if asof_shares is None:
            # This trade_date is BEFORE the earliest shares snapshot.
            # We could fall back to the EARLIEST known shares (which
            # is what yfinance does internally for marketCap pre-
            # IPO-of-tracking), but that's an extrapolation we
            # should not silently make. Skip the date.
            continue
        points.append(HistoricalMcapPoint(
            trade_date=trade_date,
            close_price=close_price,
            shares_outstanding=asof_shares,
        ))

    if not points:
        raise HistoricalMcapFetchPermanent(
            f"{yf_symbol}: no trade dates in [{from_date}, {to_date}] "
            f"had both price and asof-shares available"
        )

    return points


# ---------- internals ---------- #


def _classify_yfinance_exception(
    yf_symbol: str, e: Exception,
) -> HistoricalMcapFetchError:
    """Map raw exception → typed subtype, same as the other Phase
    2/3 fetchers."""
    msg = str(e).lower()
    if "rate" in msg and "limit" in msg:
        return HistoricalMcapFetchRateLimited(f"{yf_symbol}: {e}")
    if any(t in msg for t in (
        "timeout", "timed out", "connection reset",
        "connection refused", "remote end closed",
    )):
        return HistoricalMcapFetchTransient(f"{yf_symbol}: {e}")
    if any(t in msg for t in (
        "delisted", "no data", "symbol may be",
        "not found", "404",
    )):
        return HistoricalMcapFetchPermanent(f"{yf_symbol}: {e}")
    return HistoricalMcapFetchTransient(f"{yf_symbol}: {e}")


# ---------- type-coercion helpers ---------- #


def _is_empty(df) -> bool:
    if df is None:
        return True
    return getattr(df, "empty", True)


def _to_date(ts) -> date | None:
    """Convert a pandas Timestamp / datetime / string to date.
    Returns None if conversion fails."""
    if hasattr(ts, "date"):
        try:
            return ts.date()
        except Exception:
            return None
    if isinstance(ts, date):
        return ts
    try:
        from datetime import datetime as _dt
        return _dt.strptime(str(ts)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _to_positive_int(v) -> int | None:
    """Coerce a numpy/pandas number to positive int.

    Returns None for NaN, None, zero, negative, or unparseable.
    Same defensive logic as the shares fetcher — zero shares is
    nonsense for any real ticker; treat as missing."""
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


def _coerce_positive_float(v) -> float | None:
    """Coerce to positive float. None for NaN, zero, negative."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    if f <= 0:
        return None
    return f
