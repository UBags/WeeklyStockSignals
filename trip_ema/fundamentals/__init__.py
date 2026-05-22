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
from datetime import date, datetime
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

# Allowed values for `instrument_shares.shares_source`. Added in
# migration 0008 (Phase 2). YFINANCE is the direct `sharesOutstanding`
# field; YFINANCE_IMPLIED is the back-derived `impliedSharesOutstanding`
# used as fallback when the direct field is NULL — lower trust, so
# recorded distinctly. NSE_XBRL is reserved for the future XBRL sidecar
# (deferred — see docs/FUNDAMENTALS_PHASES_1_TO_8.md §14).
SHARES_SOURCES = (
    "YFINANCE", "YFINANCE_IMPLIED",
    "NSE_XBRL", "MANUAL_OVERRIDE",
)

# Allowed values for `quarterly_financials.source`. Added in
# migration 0009 (Phase 3). YFINANCE is direct from
# quarterly_income_stmt; YFINANCE_COMPUTED is reserved for Phase 3.B
# (computed EBITDA from operating income + D&A); NSE_XBRL for the
# future XBRL sidecar; MANUAL_OVERRIDE for operator-set values.
FINANCIAL_SOURCES = (
    "YFINANCE", "YFINANCE_COMPUTED",
    "NSE_XBRL", "MANUAL_OVERRIDE",
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


@dataclass(frozen=True, slots=True)
class SharesOutstanding:
    """A single row from instrument_shares.

    Composite PK on (ticker_id, as_of_date) — every refresh inserts
    a new snapshot rather than overwriting, so we have a time series
    of shares-outstanding per ticker.

    as_of_date is the date of the refresh (when yfinance was queried),
    NOT the source filing date. yfinance does not expose the filing
    date the number was derived from. For the XBRL sidecar (deferred),
    as_of_date will be the SHP filing date instead.

    shares_outstanding may be NULL when neither sharesOutstanding nor
    impliedSharesOutstanding was populated for this ticker — the row
    still exists so the next refresh sees "we tried and got nothing"
    rather than "we never tried".

    free_float_factor is reserved for future XBRL/manual population;
    yfinance does not provide it for Indian tickers.
    """
    ticker_id: int
    as_of_date: date
    shares_outstanding: Optional[int]
    free_float_factor: Optional[float]
    shares_source: str
    fetched_at: datetime
    xbrl_url: Optional[str] = None


@dataclass(frozen=True, slots=True)
class QuarterlyFinancial:
    """A single row from quarterly_financials.

    Composite PK is (ticker_id, fiscal_quarter, fetched_at) — the
    time-series shape means one (ticker, quarter) pair can have
    multiple rows reflecting restatements or yfinance data
    revisions. Reads default to the latest fetched_at per
    (ticker, quarter); the history is queryable for analytics that
    care about restatement detection.

    revenue and ebitda are NUMERIC(20,2) at the DB level; we cast to
    float here because pandas/numpy code downstream prefers float.
    A future refactor could move to decimal.Decimal but that's a
    project-wide change.

    fiscal_quarter is the quarter-end date AS REPORTED BY YFINANCE.
    We do not align to a global calendar — Indian companies file
    Mar/Jun/Sep/Dec quarter-ends; some yfinance entries use calendar
    Q-ends, some use fiscal. Trust what yfinance returns.

    Both revenue and ebitda may be NULL: revenue is rarely missing
    for any company yfinance knows about; ebitda is missing for
    many mid/small caps. Phase 3.A stores NULL when missing rather
    than computing it (deferred to Phase 3.B after probing).
    """
    ticker_id: int
    fiscal_quarter: date
    fetched_at: datetime
    revenue: Optional[float]
    ebitda: Optional[float]
    currency: str
    source: str


# Allowed values for `daily_market_cap.source`. Added in migration
# 0010 (Phase 4A). COMPUTED = close × shares (trusted);
# YFINANCE_HIST_MCAP = yfinance's pre-baked historical mcap series;
# APPROX_TODAYS_SHARES = close × today's shares (wrong if corporate
# actions intervened, marked for filter); MANUAL_OVERRIDE = operator.
MARKET_CAP_SOURCES = (
    "COMPUTED", "YFINANCE_HIST_MCAP",
    "APPROX_TODAYS_SHARES", "MANUAL_OVERRIDE",
)


@dataclass(frozen=True, slots=True)
class DailyMarketCap:
    """A single row from daily_market_cap.

    market_cap is in the ticker's reporting currency — for NSE/BSE
    tickers this is INR. We don't store currency on this table
    because mcap is implicitly in the same currency as `close_price`
    and `shares_used`; downstream code joining against the bars
    table should not need a currency lookup.

    shares_used is NULL when source = YFINANCE_HIST_MCAP (yfinance
    gives the mcap directly without exposing the shares-count used
    in its derivation).
    """
    ticker_id: int
    trade_date: date
    close_price: float
    shares_used: Optional[int]
    market_cap: float
    source: str
    computed_at: datetime
