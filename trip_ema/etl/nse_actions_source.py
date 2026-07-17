"""Port for the nselib corporate-actions feed.

nselib is imported ONLY here, lazily, per the closed-seam rule that keeps
third-party libraries out of module bodies (same discipline as `ports.yf_download`
for yfinance). The loader depends on the injected `NseActionsDownloader`
callable, so it is testable without nselib installed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# (from_date, to_date) -> list of feed-row dicts. Dates are 'DD-MM-YYYY',
# the format nselib.capital_market expects.
NseActionsDownloader = Callable[[str, str], list[dict[str, Any]]]


def nselib_download(from_date: str, to_date: str) -> list[dict[str, Any]]:
    """Production downloader. nselib is imported lazily — the package is only
    needed when a refresh actually fires, and the test suite must not require
    it to be installed.

    Returns the full NSE corporate-actions feed as a list of row dicts (columns
    incl. isin, symbol, exDate, faceVal, series, subject, and the various
    book-closure / no-delivery / record dates).
    """
    from nselib import capital_market  # deferred: heavy, network-bound

    df = capital_market.corporate_actions_for_equity(
        from_date=from_date, to_date=to_date
    )
    # DataFrame -> plain dicts so the loader carries no pandas dependency.
    return df.to_dict("records")
