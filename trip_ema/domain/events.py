"""Event and notification types — what gets persisted and sent to subscribers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .ohlcv import TickerRef
from .state import ChangeSignal, Timeframe


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """A single signal-change occurrence. Written to `signal_events` table.

    `bar_timestamp` is the timestamp of the bar that produced the change.
    `computed_at` is when the ComputeScheduler ran. They differ for two reasons:
    (a) the scheduler runs hourly while bars may be weekly, (b) the scheduler
    may be triggered manually or run on the wall-clock hour after a bar.
    """

    ticker: TickerRef
    timeframe: Timeframe
    bar_timestamp: datetime
    signal: ChangeSignal
    price_at_signal: float
    computed_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationBatch:
    """A bundle of events to send to a single subscriber in one message.

    Composed by the SubscriberDistributor when a subscriber's cadence
    elapses; rendered into the transport-specific format (email body /
    JSON array / Slack blocks) at the transport boundary.
    """

    subscriber_id: int
    events: list[SignalEvent]
    composed_at: datetime
