"""ETL domain types.

Frozen dataclasses, no I/O, no imports from other trip_ema modules
except domain primitives. Mirrors the trip_ema/domain pattern.

These types are the contract between the fetcher, reconciler, and
status repo. If you find yourself reaching into a tuple or dict where
one of these would do, add the missing field rather than passing
loose values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class InstrumentType(str, Enum):
    EQ = "EQ"
    ETF = "ETF"


class Tier(int, Enum):
    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3


@dataclass(frozen=True, slots=True)
class NseListing:
    """One row from EQUITY_L.csv or eq_etfseclist.csv after parsing.

    The CSVs have different column sets; this is the union normalized
    to what we care about. Fields the source CSV doesn't provide are
    None.
    """
    symbol: str
    instrument_type: InstrumentType
    isin: Optional[str]
    full_name: Optional[str]
    listing_date: Optional[date]
    face_value: Optional[float]
    lot_size: Optional[int]


@dataclass(frozen=True, slots=True)
class RawBar:
    """A single OHLCV+adjclose row as returned by yfinance, post-parse.

    Distinct from trip_ema.domain.OHLCVBar because:
      - Includes adj_close (the engine's domain type doesn't).
      - Uses date, not datetime — the bar is identified by trading-day
        in the exchange's local timezone; the timestamp conversion to
        UTC happens at write time.
    """
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: float


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """Result of fetching one ticker. Used by the reconciler and
    status updater.

    `bars` is empty when the fetch failed; check `error_message` to
    distinguish "no new bars available" (success, empty list) from
    "the fetch errored out" (`error_message` non-None).
    """
    yfinance_symbol: str
    bars: tuple[RawBar, ...] = field(default_factory=tuple)
    error_message: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.error_message is None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Outcome of reconciler.reconcile_and_write() for one ticker.

    `bars_written` is the number of rows inserted/updated. `divergences`
    is the count of old-bar differences that were detected and routed
    to corporate_actions as 'pending_review' — non-zero values warrant
    human attention.
    """
    ticker_id: int
    bars_written: int
    divergences_flagged: int
    skipped: int
    notes: Optional[str] = None


class RunKind(str, Enum):
    """Discriminator for etl_runs.kind. Each CLI subcommand maps to one."""
    LISTINGS = "listings"
    BACKFILL = "backfill"
    EOD = "eod"
    INTRADAY = "intraday"
    TIER_RECOMPUTE = "tier_recompute"
    UNPARK = "unpark"
    APPROVE_ACTION = "approve_action"
    SIGNAL_BACKTEST = "signal_backtest"
    """Phase 15a — historical signal generation for backtesting.
    The runner replays each (ticker, timeframe) through a fresh
    TickerEngine and writes the results to a per-exchange JSON file.
    Async on etl_executor (Phase 15b HTTP route); synchronous from
    the CLI (`trip-ema-etl generate-signals`)."""


class RunOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
