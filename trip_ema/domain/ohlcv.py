"""OHLCV and ticker reference types.

These are the lowest-level domain types. They have no logic, only structure.
Every other module in the system imports from `domain` but `domain` imports
from nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TickerRef:
    """Identifies a tradeable instrument across the system.

    `exchange` and `symbol` together must be unique; `instrument` distinguishes
    EQ vs INDEX vs FUT for the same symbol on the same exchange (e.g.,
    NSE:NIFTY as EQ vs INDEX vs FUT).
    """

    exchange: str
    symbol: str
    instrument: str = "EQ"

    def __str__(self) -> str:  # pragma: no cover — trivial
        return f"{self.exchange}:{self.symbol}"


@dataclass(frozen=True, slots=True)
class OHLCVBar:
    """A single bar at some timeframe.

    `timestamp` must be timezone-aware. Internal convention is UTC; timezone
    conversions happen only at the display boundary (notification rendering).
    """

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError(
                "OHLCVBar.timestamp must be timezone-aware; "
                "got a naive datetime"
            )
