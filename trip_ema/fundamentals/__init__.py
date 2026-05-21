"""Domain dataclasses for the fundamentals/indices vertical slice.

These are the immutable shapes the repositories return and the runners
consume. Keeping them in their own module (separate from the repo
files) means the GUI client and the HTTP layer can import them without
pulling SQLAlchemy into their dependency tree.

Phase 1 ships:
  - IndustryClassification
  - IndustryOverride
  - FundamentalsRun

Later phases add SharesOutstanding, QuarterlyFinancials, DailyMarketCap,
IndustryIndex, IndustryIndexConstituent, IndustryIndexValue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# Allowed values for `instrument_industry.classification_source`. Kept
# as a tuple of literals (not an Enum) because the column is VARCHAR
# in the DB; adding an Enum would force a translation layer at every
# read/write boundary for negligible benefit.
#
# 'INSTRUMENT_TYPE' was added in migration 0007 for the case where we
# classify a ticker by looking at tickers.instrument_type rather than
# calling yfinance (e.g. ETFs go straight to industry='ETF' without
# an HTTP request).
CLASSIFICATION_SOURCES = (
    "YFINANCE", "GROWW", "MANUAL_OVERRIDE",
    "NSE_SECTORAL", "INSTRUMENT_TYPE",
)

# Allowed values for `fundamentals_run.phase`.
RUN_PHASES = (
    "industry", "shares", "financials",
    "market_cap", "index_build", "index_value", "index_indicators",
)

# Allowed values for `fundamentals_run.status`.
RUN_STATUSES = ("RUNNING", "SUCCESS", "PARTIAL", "FAILED")


@dataclass(frozen=True, slots=True)
class IndustryClassification:
    """A single row from instrument_industry.

    For equities: sector + industry are populated from yfinance,
    etf_long_name is None.
    For ETFs: sector = industry = 'ETF', etf_long_name is the
    human-readable name from yfinance (e.g. 'Nippon India ETF
    Nifty IT'), classification_source = 'INSTRUMENT_TYPE'.
    """
    ticker_id: int
    sector: Optional[str]
    industry: Optional[str]
    classification_source: str
    fetched_at: datetime
    updated_at: datetime
    etf_long_name: Optional[str] = None


@dataclass(frozen=True, slots=True)
class IndustryOverride:
    """A single row from industry_override.

    Presence of a row here means the runner will skip this ticker
    on auto-refresh. Both `sector` and `industry` are NOT NULL on
    the override path because the operator is consciously asserting
    a classification.
    """
    ticker_id: int
    sector: str
    industry: str
    note: Optional[str]
    set_by: Optional[str]
    set_at: datetime


@dataclass(frozen=True, slots=True)
class FundamentalsRun:
    """A single row from fundamentals_run.

    Snapshot of a refresh invocation. `progress_json` is a free-form
    dict the runner uses for resume cursors; the repo doesn't parse
    it.
    """
    run_id: int
    phase: str
    exchange: Optional[str]
    started_at: datetime
    completed_at: Optional[datetime]
    status: str
    succeeded: int
    failed: int
    skipped: int
    progress_json: Optional[dict]
    notes: Optional[str]
