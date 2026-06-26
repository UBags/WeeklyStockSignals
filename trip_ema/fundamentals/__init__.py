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
    # V1 clean-pipeline phases (Steps 4–8 of clean-pipeline V1 refactor).
    # Each is the audit-row phase value for one stage of the three-runner
    # pipeline (state populator + enrichment populator, per series type).
    "v1_stock_indicator_state",
    "v1_stock_indicator_enrichment",
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
    sector: str | None
    industry: str | None
    classification_source: str
    fetched_at: datetime
    updated_at: datetime
    etf_long_name: str | None = None


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
    note: str | None
    set_by: str | None
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
    exchange: str | None
    started_at: datetime
    completed_at: datetime | None
    status: str
    succeeded: int
    failed: int
    skipped: int
    progress_json: dict | None
    notes: str | None


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
    shares_outstanding: int | None
    free_float_factor: float | None
    shares_source: str
    fetched_at: datetime
    xbrl_url: str | None = None


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
    revenue: float | None
    ebitda: float | None
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
    shares_used: int | None
    market_cap: float
    source: str
    computed_at: datetime


# Allowed values for industry_index.methodology. Added in migration
# 0011 (Phase 4B-1). MODIFIED_LASPEYRES is the only one currently
# implemented; the others are reserved for future indices.
INDEX_METHODOLOGIES = (
    "MODIFIED_LASPEYRES", "EQUAL_WEIGHTED", "FREE_FLOAT_WEIGHTED",
)

# Allowed values for industry_index.rebalance_schedule. Added in
# migration 0011. SEMIANNUAL_APR_OCT is the only one currently
# implemented.
REBALANCE_SCHEDULES = (
    "SEMIANNUAL_APR_OCT", "QUARTERLY", "ANNUAL_APR", "MANUAL",
)

# Allowed values for industry_index.status.
INDEX_STATUSES = ("ACTIVE", "RETIRED")

# Allowed values for industry_index_constituent.removed_reason
# (when effective_to is set).
CONSTITUENT_REMOVED_REASONS = (
    "REBALANCE_EXIT", "DELISTED", "OPERATOR_REMOVED",
)


@dataclass(frozen=True, slots=True)
class IndustryIndex:
    """An indexed yfinance industry.

    industry_name is the natural key — yfinance industry name like
    "Specialty Industrial Machinery". sector_name is the broader
    yfinance sector. methodology, rebalance_schedule, and
    min_constituents are stored per-index so future changes to
    global defaults don't retroactively invalidate existing
    indices.

    base_date is the first rebalance date — index values for trade
    dates before this don't exist (per the §20 design decision).
    """
    industry_name: str
    sector_name: str
    methodology: str
    base_value: float
    base_date: date
    rebalance_schedule: str
    min_constituents: int
    status: str
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class IndustryIndexConstituent:
    """One constituent of an industry index for one effective
    period.

    effective_to is NULL while the constituent is active in the
    index; set to the day before the next rebalance (or to
    delisted_at for mid-cycle delistings).

    weight_at_rebalance is fixed for this effective period. Mid-
    cycle delistings are handled at value-computation time via
    drop-and-renormalize — they do NOT mutate this row.

    market_cap_at_rebalance and close_at_rebalance are stored
    rather than re-derived because the modified Laspeyres formula
    references close_i(t_rebalance) for every value computation
    in the period; pre-storing avoids re-lookup.
    """
    industry_name: str
    ticker_id: int
    rebalance_date: date
    effective_from: date
    effective_to: date | None
    weight_at_rebalance: float
    market_cap_at_rebalance: float
    close_at_rebalance: float
    removed_reason: str | None = None


@dataclass(frozen=True, slots=True)
class IndustryIndexValue:
    """One day's index value plus the audit metadata.

    constituents_used + constituents_missing = total constituents
    in the active rebalance period. `renormalized` is TRUE if any
    were missing and weights were re-normalized across the
    remaining ones.

    effective_rebalance_date lets Phase 5+ analytics correlate
    value movements with constituent changes (e.g. "the IT index
    jumped 3% on 2025-10-01 because the rebalance brought in TCS
    at a 15% weight").
    """
    industry_name: str
    trade_date: date
    value: float
    constituents_used: int
    constituents_missing: int
    renormalized: bool
    effective_rebalance_date: date
    computed_at: datetime


# Allowed values for indicator_state.{core,display}_regime.
# Added in migration 0014 (Phase 5-1).
REGIME_VALUES = (-1, 0, 1)

# Allowed pattern labels on indicator_state.*_pattern_label.
# These mirror the operator's Pine Script labels exactly.
PATTERN_LABELS = (
    "NA",
    "1 SUCCESSIVE HIGHER VALLEY",
    "2 SUCCESSIVE HIGHER VALLEYS",
    "1 SUCCESSIVE LOWER TOP",
    "2 SUCCESSIVE LOWER TOPS",
)


@dataclass(frozen=True, slots=True)
class IndicatorState:
    """One row from indicator_state.

    Polymorphic series identity: either (ticker_id, timeframe) for
    a ticker bar series, or industry_name for an industry index.
    Exactly one of the two is non-None — the DB CHECK constraint
    enforces this and the dataclass relies on application code
    constructing instances correctly.

    Most fields mirror the Pine Script. A few notes on the less-
    obvious ones:

    - `display_regime`: -1 / 0 / +1. core is the
      underlying state; display is the gated state that downstream
      strategy consumers actually read.

    - `dist_to_*_in_hl_units`: distance from price to the
      respective EMA, measured in units of the 26-period SMA of
      (high - low). The operator's table uses this as a
      normalized "how far from the EMA are we" metric.

    - `mean_dist_to_*_in_hl_units`: the 52-period SMA of the above.
      Used in the operator's table to colorize the current
      distance (green if below mean, red if above) — the idea
      being "is the price closer to the EMA than usual?"

    - `bars_since_*_turn`: bars since each EMA last changed slope
      direction. NULL until each EMA has had at least one turn.

    - `pct_change_since_*_turn`: signed percent change in close
      from the bar where each EMA last turned.

    - `*_turn_buy` / `*_turn_sell`: TRUE on the bar where the EMA
      changes direction (buy = slope flipped from -→+; sell =
      slope flipped from +→-). Used by signal aggregation.

    - `*_pattern_label`: one of PATTERN_LABELS. The operator's
      "higher valleys / lower tops" pattern detection per EMA.
    """
    # --- polymorphic identity ---
    ticker_id: int | None
    timeframe: str | None
    industry_name: str | None
    trade_date: date

    # --- EMAs ---
    med_ema: float
    long_ema: float
    longest_ema: float

    # --- ATR + bands ---
    atr: float
    upper_inner_band: float
    lower_inner_band: float
    upper_mid_band: float
    lower_mid_band: float
    upper_outer_band: float
    lower_outer_band: float

    # --- regime ---
    display_regime: int

    # --- pierces ---
    pierces_upper_mid: bool
    pierces_lower_mid: bool

    # --- per-EMA distances ---
    dist_to_med_in_hl_units: float | None
    dist_to_long_in_hl_units: float | None
    dist_to_longest_in_hl_units: float | None
    mean_dist_to_med_in_hl_units: float | None
    mean_dist_to_long_in_hl_units: float | None
    mean_dist_to_longest_in_hl_units: float | None

    # --- bars-since + pct-change-since per EMA ---
    bars_since_med_turn: int | None
    bars_since_long_turn: int | None
    bars_since_longest_turn: int | None
    pct_change_since_med_turn: float | None
    pct_change_since_long_turn: float | None
    pct_change_since_longest_turn: float | None

    # --- pattern labels ---
    med_pattern_label: str
    long_pattern_label: str
    longest_pattern_label: str

    # --- raw turn signals at this bar ---
    med_turn_buy: bool
    med_turn_sell: bool
    long_turn_buy: bool
    long_turn_sell: bool
    longest_turn_buy: bool
    longest_turn_sell: bool

    # --- audit ---
    computed_at: datetime


# Allowed values for signal_events.signal_type. Phase 6-1 used
# the short forms 'EXIT_BUY' / 'EXIT_SELL' on its own signal_event
# table; migration 0016 unifies into signal_events whose existing
# value set is the long form 'EXIT_FROM_BUY' / 'EXIT_FROM_SELL' —
# matching the live ComputeScheduler path. We standardize on the
# long forms here, and signal_compute.py emits accordingly.
SIGNAL_EVENT_KINDS = ("BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL")


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """One row from signal_events.

    Sparse — only bars where a regime transition fired produce
    rows. event_kind tells you which of the four signal types it is:
      - BUY       : entered display_regime=+1 from non-+1
      - SELL      : entered display_regime=-1 from non--1
      - EXIT_BUY  : left display_regime=+1
      - EXIT_SELL : left display_regime=-1

    Always identifies a ticker bar series via (ticker_id, timeframe).
    Industry-level signal events were removed in A1 demolition;
    industry-level state still lives in indicator_state but no
    longer produces signal events.

    regime_before and regime_after are the display_regime values
    on the previous bar and this bar respectively. Constrained to
    {-1, 0, +1} at the DB level.

    close_at_event is the close on the bar where the event fired.
    Useful for downstream backtesting without needing a JOIN back
    to ohlcv_bars.
    """
    # --- series identity ---
    ticker_id: int
    timeframe: str
    trade_date: date

    # --- event ---
    event_kind: str  # one of SIGNAL_EVENT_KINDS

    # --- regime context ---
    regime_before: int
    regime_after: int

    # --- price context ---
    close_at_event: float

    # --- audit ---
    computed_at: datetime

# Allowed values for stock_benchmark_stats.benchmark_kind (Phase 6-3).
BENCHMARK_KINDS = ("INDUSTRY", "NIFTY")

# Allowed values for window_label on both benchmark stats tables.
# These map to fixed observation counts (26 / 52 / 156 weekly returns)
# defined as WINDOW_RETURNS in benchmark_stats_compute.py.
WINDOW_LABELS = ("SHORT", "MEDIUM", "LONG")

# Concrete benchmark_id for the NIFTY proxy. Same constant used by
# the IndustryPanel fallback (_NIFTY50_FALLBACK).
NIFTY_BENCHMARK_ID = "NIFTY"


@dataclass(frozen=True, slots=True)
class StockBenchmarkStats:
    """One row from stock_benchmark_stats.

    Per-(stock, benchmark, window, as_of) regression snapshot.
    Composite PK is enforced at the DB level; the dataclass mirrors
    every column.

    Metric fields are Optional[float] because runners write None for
    stocks with too few observations (n_weeks < 8) and for the
    degenerate-zero-variance case.

    benchmark_kind disambiguates "industry" (benchmark_id =
    industry_name) from "nifty" (benchmark_id = 'NIFTY'). Carrying
    both fields makes the row self-describing — JOINs don't need
    to peek at industry_index to interpret benchmark_id.
    """
    ticker_id: int
    benchmark_kind: str               # one of BENCHMARK_KINDS
    benchmark_id: str                 # industry_name OR 'NIFTY'
    window_label: str                 # one of WINDOW_LABELS
    as_of_date: date
    n_weeks: int
    beta: float | None
    alpha_annual: float | None
    r_squared: float | None
    sigma_annual: float | None
    rs_pct: float | None
    info_ratio: float | None
    computed_at: datetime


@dataclass(frozen=True, slots=True)
class IndustryBenchmarkStats:
    """One row from industry_benchmark_stats.

    Mirror of StockBenchmarkStats minus ticker_id and benchmark_kind
    (industries are always vs NIFTY in Phase 6-3; if a future phase
    adds industry-vs-industry comparisons, add a benchmark_kind column
    via migration and a discriminator field here).
    """
    industry_name: str
    benchmark_id: str                 # 'NIFTY'
    window_label: str
    as_of_date: date
    n_weeks: int
    beta: float | None
    alpha_annual: float | None
    r_squared: float | None
    sigma_annual: float | None
    rs_pct: float | None
    info_ratio: float | None
    computed_at: datetime

