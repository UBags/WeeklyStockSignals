"""API response types.

Verbose: full per-bar state history (up to N bars).
Terse:   just the latest change signal.

These are what external callers receive. Kept separate from `domain.state`
so the domain types stay focused on the internal computation and these
stay focused on the public contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..domain import BarState, ChangeSignal, TickerRef, Timeframe


@dataclass(frozen=True, slots=True)
class TerseResponse:
    """The minimum viable response — what changed, when, at what price."""

    ticker: TickerRef
    timeframe: Timeframe
    latest_bar_timestamp: datetime
    latest_change: ChangeSignal
    latest_price: float
    computed_at: datetime


@dataclass(frozen=True, slots=True)
class VerboseResponse:
    """Full per-bar state for the last N bars, plus the latest change."""

    ticker: TickerRef
    timeframe: Timeframe
    bars: tuple[BarState, ...]              # oldest first; latest last
    latest_change: ChangeSignal
    computed_at: datetime


class TickerNotPrimedError(Exception):
    """Raised when a request targets a ticker that isn't in the warm
    registry. The caller should either prime it via compute_full() or
    add it to the DB and restart the app."""


class InsufficientHistoryError(Exception):
    """Raised when a stateless compute is requested with too few bars
    for the EMAs to warm up. Threshold is conservative — at least
    `longest_base + longest_s1 + longest_s2 + sqrt(longest_s2)` bars."""
