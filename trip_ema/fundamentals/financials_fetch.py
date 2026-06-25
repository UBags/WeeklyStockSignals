"""yfinance Ticker.quarterly_income_stmt → list[QuarterlyFinancial]
for a single symbol.

Same pattern as industry_fetch.py and shares_fetch.py — one HTTP per
ticker, typed exceptions for rate-limit / transient / permanent
failures so the runner can apply differentiated retry policy.

yfinance returns quarterly financials as a pandas DataFrame indexed
by line-item name with columns being quarter-end dates. We pick out
the rows we care about:

  - Revenue: tried under several candidate names because yfinance's
    field naming varies across companies and yfinance versions.
    "Total Revenue" is most common; "Revenues" and
    "Revenue From Operations" appear too (the latter is the Ind AS
    statutory line name for Indian companies).
  - EBITDA: tried under "EBITDA" and "Normalized EBITDA". Phase 3.A
    stores NULL when neither is populated; Phase 3.B (deferred)
    will compute it from operating income + D&A after a proper
    probe of the yfinance field shape.

The fetcher returns ONE record per quarter that yfinance has data
for, up to `max_quarters` (default 4). The runner is responsible for
deciding which quarters to write to the DB.

What this module deliberately does NOT do:
  - DB writes. The runner does.
  - EBITDA computation from operating income + D&A. Deferred to
    Phase 3.B after probing yfinance's field reliability.
  - Currency conversion. We trust yfinance's reported currency
    (`financialCurrency` or default INR for NSE/BSE).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from ..etl.exchanges import yfinance_symbol

logger = logging.getLogger(__name__)


# ---------- Public dataclass ---------- #


@dataclass(frozen=True, slots=True)
class QuarterlyFinancialInfo:
    """One quarter's revenue + EBITDA from yfinance.

    All fields except fiscal_quarter may be None: revenue is usually
    populated for any company yfinance knows about; ebitda is often
    NULL. The runner stores whatever's here without computing
    missing values.
    """
    fiscal_quarter: date
    revenue: float | None
    ebitda: float | None
    currency: str  # "INR" default for NSE/BSE


# ---------- Typed exceptions ---------- #


class FinancialsFetchError(Exception):
    """Base class. Runner catches this to count a ticker as failed."""


class FinancialsFetchRateLimited(FinancialsFetchError):
    """yfinance signaled rate limit. Runner applies the longer
    cool-off."""


class FinancialsFetchTransient(FinancialsFetchError):
    """Network/HTTP transient failure. Retryable in principle."""


class FinancialsFetchPermanent(FinancialsFetchError):
    """The ticker doesn't exist on Yahoo, OR yfinance returned
    quarterly financials with no usable quarters (empty DataFrame).
    Runner won't retry until next quarterly refresh."""


# ---------- The fetch function ---------- #


# Candidate row names for revenue. Tried in order; first non-NaN wins.
# Order matters: "Total Revenue" is the canonical yfinance name and
# usually matches; the others are fallbacks for companies where
# yfinance has surfaced a different statutory label.
REVENUE_FIELD_CANDIDATES = (
    "Total Revenue",
    "Revenues",
    "Revenue",
    "Revenue From Operations",
    "Operating Revenue",
)

EBITDA_FIELD_CANDIDATES = (
    "EBITDA",
    "Normalized EBITDA",
)


def fetch_quarterly_financials(
    symbol: str, exchange: str, *, max_quarters: int = 4,
) -> list[QuarterlyFinancialInfo]:
    """Fetch up to `max_quarters` of quarterly revenue + EBITDA.

    Returns a list of QuarterlyFinancialInfo, newest quarter first,
    up to `max_quarters` long. Quarters with NEITHER revenue nor
    EBITDA populated are filtered out — the caller doesn't need a
    record of "we tried this quarter and got nothing for both".
    Quarters with at least one of the two populated are kept so the
    runner can record them.

    Raises:
      FinancialsFetchRateLimited / Transient / Permanent — same
      semantics as the other Phase 2/3 fetchers.
    """
    yf_symbol = yfinance_symbol(symbol, exchange)
    try:
        df, currency = _fetch_yfinance_quarterly(yf_symbol)
    except (FinancialsFetchRateLimited, FinancialsFetchTransient,
            FinancialsFetchPermanent):
        raise
    except Exception as e:
        raise _classify_yfinance_exception(yf_symbol, e) from e

    if df is None or _is_empty(df):
        raise FinancialsFetchPermanent(
            f"{yf_symbol}: yfinance returned no quarterly financials"
        )

    # Extract revenue and ebitda rows by trying each candidate name.
    revenue_row = _first_matching_row(df, REVENUE_FIELD_CANDIDATES)
    ebitda_row = _first_matching_row(df, EBITDA_FIELD_CANDIDATES)

    # Iterate columns (quarter-end dates), newest first.
    quarters: list[QuarterlyFinancialInfo] = []
    for col in df.columns[:max_quarters]:
        fq = _normalize_quarter_date(col)
        if fq is None:
            continue
        rev = _coerce_float(revenue_row[col]) if revenue_row is not None else None
        ebt = _coerce_float(ebitda_row[col]) if ebitda_row is not None else None
        # Skip quarters where both fields are NULL — no value in
        # recording an empty row.
        if rev is None and ebt is None:
            continue
        quarters.append(QuarterlyFinancialInfo(
            fiscal_quarter=fq,
            revenue=rev,
            ebitda=ebt,
            currency=currency,
        ))

    if not quarters:
        raise FinancialsFetchPermanent(
            f"{yf_symbol}: yfinance had quarterly data but no quarters "
            f"had either Revenue or EBITDA populated"
        )

    return quarters


def _classify_yfinance_exception(yf_symbol: str, e: Exception) -> FinancialsFetchError:
    """Map raw exception to typed subtype. Same pattern as the other
    Phase 2/3 fetchers — yfinance's exception hierarchy isn't stable
    so we match strings."""
    msg = str(e).lower()
    if "rate" in msg and "limit" in msg:
        return FinancialsFetchRateLimited(f"{yf_symbol}: {e}")
    if any(t in msg for t in ("timeout", "timed out", "connection reset",
                              "connection refused", "remote end closed")):
        return FinancialsFetchTransient(f"{yf_symbol}: {e}")
    if any(t in msg for t in ("delisted", "no data", "symbol may be",
                              "not found", "404")):
        return FinancialsFetchPermanent(f"{yf_symbol}: {e}")
    return FinancialsFetchTransient(f"{yf_symbol}: {e}")


def _fetch_yfinance_quarterly(yf_symbol: str):
    """The actual yfinance call. Returns (DataFrame, currency).

    Isolated for test patchability — tests patch THIS function with
    a stub returning a fake DataFrame, so the test suite doesn't
    need yfinance installed and doesn't depend on network.
    """
    import yfinance as yf  # lazy

    ticker = yf.Ticker(yf_symbol)
    # `quarterly_income_stmt` is the newer name; `quarterly_financials`
    # is the alias in older yfinance. Prefer the new one but fall
    # back if it's not present (defensive against version skew).
    df = getattr(ticker, "quarterly_income_stmt", None)
    if df is None or _is_empty(df):
        df = getattr(ticker, "quarterly_financials", None)
    # Currency: `info` has `financialCurrency`. We pull it once;
    # default to INR for Indian exchanges which is the most common
    # case for our universe.
    currency = "INR"
    try:
        info = ticker.info or {}
        if info.get("financialCurrency"):
            currency = str(info["financialCurrency"])
    except Exception:
        # Currency fallback is acceptable; don't crash the fetch.
        pass
    return df, currency


# ---------- helpers ---------- #


def _is_empty(df) -> bool:
    """True if df is None, has no columns, or has no rows. Defensive
    against the various ways yfinance might signal 'no data'."""
    if df is None:
        return True
    # pandas: empty attr is what we want.
    return getattr(df, "empty", True)


def _first_matching_row(df, candidates):
    """Return the first row from the DataFrame whose index name
    matches one of `candidates`. Returns None if no candidate
    matches.

    `df.index` is a pandas Index of line-item names. We look up by
    exact string match; case-insensitive matching could surface
    more but also risks false positives ("Revenue From Operations"
    might match a sub-line we don't want).
    """
    idx = df.index
    for name in candidates:
        if name in idx:
            return df.loc[name]
    return None


def _normalize_quarter_date(col) -> date | None:
    """Convert a pandas/yfinance column header to a date. yfinance
    uses pandas.Timestamp for these; we want a plain date.

    Returns None if the conversion fails."""
    if hasattr(col, "date"):
        try:
            return col.date()
        except Exception:
            return None
    if isinstance(col, date):
        return col
    # Fallback: try string parsing.
    try:
        from datetime import datetime as _dt
        return _dt.strptime(str(col)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _coerce_float(v) -> float | None:
    """yfinance returns numbers as numpy floats, sometimes NaN.
    Convert to Python float or None.

    NaN is the way pandas signals "missing" in numeric columns; we
    treat it as None at the boundary.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN check (NaN != NaN).
    if f != f:
        return None
    return f
