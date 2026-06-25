"""Exchange ↔ yfinance-suffix mapping + validation helpers.

Single source of truth for which exchanges trip_ema knows how to
translate symbols for. Two distinct concepts:

  1. KNOWN exchanges: those the codebase can format yfinance
     symbols for (the `_SUFFIX_MAP` table here). Static; only
     changes when new exchanges are added to the codebase.
  2. CONFIGURED exchanges: those the operator has enabled in
     config.yaml (under `market_hours:`). Dynamic; can change
     without code edits.

Both checks gate every ETL operation: an exchange must be (1) known
to the codebase AND (2) configured by the operator. If either check
fails, `UnsupportedExchangeError` is raised.

Adding a new exchange to the codebase:
  - Add a row to `_SUFFIX_MAP` here.
  - If the exchange needs different listings ingestion (CSV format,
    URL, etc.), that's a separate file under trip_ema/etl/. The
    yfinance fetch path is already exchange-agnostic via this map.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


# ---------- Canonical exchange → yfinance suffix table ---------- #

# yfinance's symbol conventions (codified here, single source of truth):
#   NSE     'RELIANCE'  → 'RELIANCE.NS'      Indian National
#   BSE     'RELIANCE'  → 'RELIANCE.BO'      Indian Bombay
#   NYSE    'IBM'       → 'IBM'              ⚠ ambiguous with NASDAQ
#   NASDAQ  'MSFT'      → 'MSFT'             ⚠ ambiguous with NYSE
#   TSE     '7203'      → '7203.T'           Tokyo
#   HKSE    '0700'      → '0700.HK'          Hong Kong
#   SSE     '600519'    → '600519.SS'        Shanghai
#   SZSE    '000001'    → '000001.SZ'        Shenzhen
#   LSE     'BARC'      → 'BARC.L'           London
#
# Note on NYSE/NASDAQ: yfinance does not disambiguate US listings by
# exchange. Both map to the bare symbol. The `instrument` field on
# the tickers table (or an external metadata source) is the only
# disambiguation; this module is not the place to invent one.
_SUFFIX_MAP: dict[str, str] = {
    "NSE":    ".NS",
    "BSE":    ".BO",
    "NYSE":   "",
    "NASDAQ": "",
    "TSE":    ".T",
    "HKSE":   ".HK",
    "SSE":    ".SS",
    "SZSE":   ".SZ",
    "LSE":    ".L",
}


# ---------- Exceptions ---------- #


class UnsupportedExchangeError(ValueError):
    """Raised when an ETL operation is invoked for an exchange that
    is either (a) unknown to the codebase, or (b) not present in
    the live config's `market_hours:` block.

    Subclasses `ValueError` so existing `except ValueError` blocks
    still catch it; raise this specific class (not bare ValueError)
    so callers that want to handle the "exchange" failure mode
    specifically can do so."""


# ---------- Symbol mapping ---------- #


def yfinance_symbol(symbol: str, exchange: str) -> str:
    """Format a ticker symbol for yfinance.

    Example: yfinance_symbol("RELIANCE", "NSE") → "RELIANCE.NS"

    Raises UnsupportedExchangeError if `exchange` is not in the
    known suffix map. Callers should validate the exchange against
    config first (via `validate_exchange()`); this defensive check
    catches programmer errors where validation was skipped.
    """
    try:
        suffix = _SUFFIX_MAP[exchange]
    except KeyError:
        raise UnsupportedExchangeError(
            f"Exchange {exchange!r} is not in the known suffix map. "
            f"Known exchanges: {sorted(_SUFFIX_MAP)}.",
        )
    return f"{symbol.upper()}{suffix}"


def from_yfinance_symbol(yf_symbol: str, exchange: str) -> str:
    """Inverse of `yfinance_symbol`.

    Strips the exchange's suffix and returns the bare symbol.
    For exchanges with no suffix (NYSE, NASDAQ) returns the symbol
    unchanged but normalised to uppercase.

    Defensive: if the input doesn't end with the expected suffix
    (e.g. the caller mixed exchanges), returns the symbol with any
    "." suffix stripped — matches the legacy behavior. Raises
    UnsupportedExchangeError only for genuinely unknown exchanges.
    """
    if exchange not in _SUFFIX_MAP:
        raise UnsupportedExchangeError(
            f"Exchange {exchange!r} is not in the known suffix map. "
            f"Known exchanges: {sorted(_SUFFIX_MAP)}.",
        )
    suffix = _SUFFIX_MAP[exchange]
    if suffix and yf_symbol.endswith(suffix):
        return yf_symbol[: -len(suffix)].upper()
    # Fallback: strip after the first "." if any. Matches the legacy
    # `from_yfinance_symbol` behaviour for unknown-suffix inputs.
    if "." in yf_symbol:
        return yf_symbol.split(".", 1)[0].upper()
    return yf_symbol.upper()


# ---------- Validation ---------- #


def known_exchanges() -> frozenset[str]:
    """The exchanges the codebase knows how to format symbols for.
    Static — does not depend on config."""
    return frozenset(_SUFFIX_MAP.keys())


def configured_exchanges(config: Config) -> frozenset[str]:
    """The exchanges the operator has configured under `market_hours:`
    in config.yaml. Dynamic — reflects the current config."""
    if not hasattr(config, "market_hours") or not config.market_hours:
        return frozenset()
    return frozenset(mh.exchange for mh in config.market_hours)


def validate_exchange(exchange: str, config: Config) -> None:
    """Two-step validation for every ETL operation:

      1. `exchange` must be in the codebase's KNOWN_EXCHANGES
         (otherwise the suffix-mapper can't format symbols).
      2. `exchange` must be in config.market_hours
         (otherwise the operator hasn't enabled it).

    Raises UnsupportedExchangeError with a specific message
    identifying which check failed. Returns None on success.
    """
    known = known_exchanges()
    if exchange not in known:
        raise UnsupportedExchangeError(
            f"Exchange {exchange!r} is not supported by this codebase. "
            f"Known: {sorted(known)}.",
        )
    configured = configured_exchanges(config)
    if exchange not in configured:
        raise UnsupportedExchangeError(
            f"Exchange {exchange!r} is not configured. "
            f"Configured exchanges (from market_hours): "
            f"{sorted(configured) or '(none)'}. "
            f"Add it to config.yaml's `market_hours:` block to enable.",
        )
