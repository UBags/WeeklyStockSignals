"""yfinance Ticker.info → SharesInfo for a single symbol.

Same pattern as industry_fetch.py — one HTTP per ticker, typed
exceptions for rate-limit / transient / permanent failures so the
runner can apply differentiated retry policy.

Two yfinance fields are interesting here:

  - `sharesOutstanding`         — direct count from the latest filing.
                                   Trusted.
  - `impliedSharesOutstanding`  — yfinance's back-derivation from
                                   market_cap / price. CIRCULAR for our
                                   purposes (we use it to compute
                                   market_cap), but populated for some
                                   tickers where the direct field is
                                   NULL.

Policy: prefer `sharesOutstanding`. Fall back to
`impliedSharesOutstanding` only when the direct field is NULL. The
returned `SharesInfo` records which one we used in `source` so the
caller can persist provenance into instrument_shares.shares_source —
analytics can then filter on confidence level.

What this module deliberately does NOT do:
  - DB writes. The runner does that.
  - Free-float computation. yfinance doesn't expose this for Indian
    tickers; left for the (deferred) XBRL sidecar.
  - Sleep between calls. The runner does that.

This module shares its yfinance call pattern with industry_fetch but
deliberately does NOT share a fetcher function — calling Ticker.info
once and consuming both industry and shares would halve HTTP cost
but couples Phase 1 and Phase 2 such that we can't replace either
independently. The user explicitly chose separate commands for that
reason (XBRL sidecar swap-in later).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..etl.exchanges import yfinance_symbol

logger = logging.getLogger(__name__)


# ---------- Public dataclass ---------- #


@dataclass(frozen=True, slots=True)
class SharesInfo:
    """What we extract from yfinance's Ticker.info dict for shares.

    `source` is the string the caller writes to
    instrument_shares.shares_source — 'YFINANCE' for the direct
    field, 'YFINANCE_IMPLIED' for the back-derived fallback. None
    of these are NULL: if neither field was populated, the fetcher
    raises SharesFetchPermanent, it doesn't return a SharesInfo
    with shares=None.
    """
    shares_outstanding: int
    source: str  # 'YFINANCE' | 'YFINANCE_IMPLIED'


# ---------- Typed exceptions ---------- #


class SharesFetchError(Exception):
    """Base class. Runner catches this to count a ticker as failed."""


class SharesFetchRateLimited(SharesFetchError):
    """yfinance signaled rate limit. Runner applies the longer
    cool-off than for other failures."""


class SharesFetchTransient(SharesFetchError):
    """Network / transient HTTP failure. Retryable in principle;
    we don't retry inside the fetcher."""


class SharesFetchPermanent(SharesFetchError):
    """The ticker doesn't exist on Yahoo, OR yfinance returned info
    but neither sharesOutstanding nor impliedSharesOutstanding was
    populated. Runner counts these as failed; won't retry until
    next quarterly refresh."""


# ---------- The fetch function ---------- #


def fetch_shares(symbol: str, exchange: str) -> SharesInfo:
    """Fetch shares-outstanding for one ticker.

    Returns a SharesInfo with `shares_outstanding > 0` and `source`
    one of 'YFINANCE' / 'YFINANCE_IMPLIED'.

    Raises:
      SharesFetchRateLimited  — yfinance signaled rate limit.
      SharesFetchTransient    — network/HTTP transient failure.
      SharesFetchPermanent    — ticker delisted/unknown, OR both
                                 sharesOutstanding and
                                 impliedSharesOutstanding were NULL.
    """
    yf_symbol = yfinance_symbol(symbol, exchange)
    try:
        info = _fetch_yfinance_info(yf_symbol)
    except (SharesFetchRateLimited, SharesFetchTransient,
            SharesFetchPermanent):
        # Already classified by the inner function — re-raise as-is.
        raise
    except Exception as e:
        raise _classify_yfinance_exception(yf_symbol, e) from e

    # Empty info dict means delisted/unknown. Same logic as industry_fetch.
    if not info or not any(info.get(k) for k in (
        "symbol", "shortName", "longName", "quoteType",
    )):
        raise SharesFetchPermanent(
            f"{yf_symbol}: yfinance returned empty info dict "
            f"(ticker may be delisted or unknown)"
        )

    # Prefer direct sharesOutstanding.
    direct = _as_positive_int(info.get("sharesOutstanding"))
    if direct is not None:
        return SharesInfo(shares_outstanding=direct, source="YFINANCE")

    # Fall back to implied. Note: this is back-derived from
    # marketCap / price by yfinance, so it's literally circular for
    # computing our own market cap downstream. We accept it as a
    # last-resort signal of "what shares-count would explain this
    # market cap?", which is at least usable for relative comparison
    # within an industry index.
    implied = _as_positive_int(info.get("impliedSharesOutstanding"))
    if implied is not None:
        return SharesInfo(shares_outstanding=implied, source="YFINANCE_IMPLIED")

    # Neither field. Treat as permanent — there's nothing to retry.
    raise SharesFetchPermanent(
        f"{yf_symbol}: neither sharesOutstanding nor "
        f"impliedSharesOutstanding was populated"
    )


def _classify_yfinance_exception(yf_symbol: str, e: Exception) -> SharesFetchError:
    """Map a raw exception to one of our typed subtypes by message
    substring. Mirrors the industry_fetch classifier — yfinance's
    exception hierarchy isn't stable so we match strings."""
    msg = str(e).lower()
    if "rate" in msg and "limit" in msg:
        return SharesFetchRateLimited(f"{yf_symbol}: {e}")
    if any(t in msg for t in ("timeout", "timed out", "connection reset",
                              "connection refused", "remote end closed")):
        return SharesFetchTransient(f"{yf_symbol}: {e}")
    if any(t in msg for t in ("delisted", "no data", "symbol may be",
                              "not found", "404")):
        return SharesFetchPermanent(f"{yf_symbol}: {e}")
    # Unknown → treat as transient so we don't permanently park
    # tickers on novel yfinance errors.
    return SharesFetchTransient(f"{yf_symbol}: {e}")


def _fetch_yfinance_info(yf_symbol: str) -> dict:
    """The actual yfinance call, isolated for test patchability.

    Tests patch THIS function (with side_effect=Exception(...)) and
    verify that fetch_shares's outer try/except classifies correctly.
    """
    import yfinance as yf  # lazy: only required when an ETL run fires

    ticker = yf.Ticker(yf_symbol)
    return ticker.info or {}


def _as_positive_int(v) -> int | None:
    """yfinance returns numbers as int OR float OR sometimes the
    string 'None' for unset fields. Normalize to positive int or None.

    Zero is treated as "missing" because yfinance has been seen to
    return 0 for delisted/unknown tickers where the right answer is
    NULL. A real ticker with literally zero shares outstanding
    doesn't exist; treating 0 as missing is safer than recording it.
    """
    if v is None:
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    if i <= 0:
        return None
    return i
