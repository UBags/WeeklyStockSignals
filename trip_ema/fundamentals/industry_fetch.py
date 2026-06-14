"""yfinance Ticker.info → IndustryInfo for a single symbol.

This is the lowest-level Phase 1 building block. The runner above
calls `fetch_industry(symbol, exchange)` once per ticker and handles
the rate-limiting/retry/progress at the loop level.

Why one-at-a-time instead of batched: yfinance's `.info` property is
implemented as a per-ticker HTTP call to Yahoo's quote-summary
endpoint; there is no batched form. Batching is not available at
this layer — see the trip_ema.etl.yfinance_source module's
`yf.download(...)` for the batched HISTORICAL endpoint, which is
unrelated.

Failure modes the fetcher handles:
  - `YFRateLimitError`: yfinance's own rate-limit exception. The
    runner catches it and applies a longer cool-off, but we surface
    it as a typed exception so the runner can distinguish "rate
    limited, retry later" from "ticker doesn't exist".
  - HTTP timeout, connection reset, transient 5xx: caught, logged,
    treated as a failure for THIS ticker (don't pollute neighbors).
  - `.info` returns a dict but neither sector nor industry is set:
    NOT treated as failure. Returns IndustryInfo(None, None) so the
    instrument_industry row exists with NULL values; the runner
    counts this as a success because the call succeeded.

What this module deliberately does NOT do:
  - DB writes. The runner does that.
  - Sleep between calls. The runner does that (since it knows the
    cadence policy across the whole batch).
  - Symbol normalization. The runner passes (symbol, exchange) and
    we call exchanges.yfinance_symbol() to format. The bare symbol
    is opaque to us.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..etl.exchanges import yfinance_symbol

logger = logging.getLogger(__name__)


# ---------- Public dataclass ---------- #


@dataclass(frozen=True, slots=True)
class IndustryInfo:
    """What we extract from yfinance's Ticker.info dict.

    Both fields may be None. `quote_type` is a sanity check (yfinance
    returns 'EQUITY' for stocks, 'ETF' for ETFs, etc.) — useful for
    flagging mis-classified rows.
    """
    sector: Optional[str]
    industry: Optional[str]
    quote_type: Optional[str] = None


# ---------- Typed exceptions ---------- #


class IndustryFetchError(Exception):
    """Base class for fetch failures. The runner catches this and
    counts the ticker as failed."""


class IndustryFetchRateLimited(IndustryFetchError):
    """yfinance/Yahoo signaled rate limit. The runner catches this
    specifically and applies a longer cool-off than for other
    failures."""


class IndustryFetchTransient(IndustryFetchError):
    """Network/HTTP transient failure. Retryable in principle, but
    we don't retry inside the fetcher — the runner does, as part
    of its outer loop, because retry policy is a run-level concern."""


class IndustryFetchPermanent(IndustryFetchError):
    """The ticker doesn't exist on Yahoo, or some other definitive
    "this will never work" outcome. The runner counts these as
    failed and won't retry."""


# ---------- The fetch function ---------- #


def fetch_industry(symbol: str, exchange: str) -> IndustryInfo:
    """Fetch sector/industry for one ticker.

    Raises IndustryFetchRateLimited / IndustryFetchTransient /
    IndustryFetchPermanent on the corresponding failure modes.
    Returns IndustryInfo(None, None) if the call succeeded but
    yfinance has no classification for this symbol — that's a
    valid outcome to record.
    """
    yf_symbol = yfinance_symbol(symbol, exchange)
    try:
        info = _fetch_yfinance_info(yf_symbol)
    except (IndustryFetchRateLimited, IndustryFetchTransient,
            IndustryFetchPermanent):
        # Already classified by the inner function — re-raise as-is.
        raise
    except Exception as e:
        # yfinance raises different exception types across versions.
        # We map by message-substring because yfinance's exception
        # hierarchy isn't stable. Doing this here rather than inside
        # _fetch_yfinance_info means tests can patch the inner
        # function with a raw Exception and verify the classifier
        # picks the right typed exception.
        raise _classify_yfinance_exception(yf_symbol, e) from e

    # yfinance .info returns {} for delisted/nonexistent tickers in
    # some versions. Distinguish that from "valid ticker, no industry"
    # by looking for ANY identifying field.
    if not info or not any(info.get(k) for k in (
        "symbol", "shortName", "longName", "quoteType",
    )):
        raise IndustryFetchPermanent(
            f"{yf_symbol}: yfinance returned empty info dict "
            f"(ticker may be delisted or unknown)"
        )

    sector = _clean_string(info.get("sector"))
    industry = _clean_string(info.get("industry"))
    quote_type = _clean_string(info.get("quoteType"))

    return IndustryInfo(sector=sector, industry=industry, quote_type=quote_type)


def _classify_yfinance_exception(yf_symbol: str, e: Exception) -> IndustryFetchError:
    """Map a raw exception to one of our typed subtypes by message
    substring. Public-ish: tests for fetch_industry exercise this
    path by patching _fetch_yfinance_info to raise raw Exceptions."""
    msg = str(e).lower()
    if "rate" in msg and "limit" in msg:
        return IndustryFetchRateLimited(f"{yf_symbol}: {e}")
    if any(t in msg for t in ("timeout", "timed out", "connection reset",
                              "connection refused", "remote end closed")):
        return IndustryFetchTransient(f"{yf_symbol}: {e}")
    # Yahoo returning 404 / "Symbol may be delisted" lands here.
    if any(t in msg for t in ("delisted", "no data", "symbol may be",
                              "not found", "404")):
        return IndustryFetchPermanent(f"{yf_symbol}: {e}")
    # Unknown — treat as transient so we don't permanently park
    # tickers on novel yfinance errors. The runner won't retry
    # immediately, but the next quarterly refresh will pick it up.
    return IndustryFetchTransient(f"{yf_symbol}: {e}")


def _fetch_yfinance_info(yf_symbol: str) -> dict:
    """The actual yfinance call, isolated for test patchability.

    Tests patch THIS function (with side_effect=Exception(...)) and
    verify that fetch_industry's outer try/except classifies the
    raw exception to the right typed subtype.
    """
    import yfinance as yf  # lazy: only required when an ETL run fires

    ticker = yf.Ticker(yf_symbol)
    return ticker.info or {}


def _clean_string(v) -> Optional[str]:
    """yfinance sometimes returns empty strings, the literal string
    'None', or whitespace. Normalize all of those to None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "none":
        return None
    return s
