"""V1 SignalEvent — the persistence-layer signal dataclass.

One row from `current_signals` OR `historical_signals` (same shape).
PK semantics differ between the tables — `current_signals` has
`ticker_id` as the sole PK (max one row per ticker, this cycle
only); `historical_signals` has `signal_id BIGSERIAL` and no other
uniqueness (append-only audit log).

This dataclass intentionally OMITS:
  - `signal_id` — persistence-internal to historical_signals; not
    part of the signal's domain identity.
  - `archived_at` — historical_signals audit field; readers who
    need it query the table directly.

Coexists during the V1 cutover with two other `SignalEvent` classes:
  - `trip_ema.domain.events.SignalEvent` — legacy in-memory event
    for the subscriber_distributor / notifier_worker pipeline (going
    away in Step 14).
  - `trip_ema.fundamentals.SignalEvent` — legacy persistence-layer
    dataclass for the `signal_events` table (going away in Step 15
    when the table is dropped).

V1 callers import via the full module path:
    from trip_ema.domain.signal_event import SignalEvent
This avoids ambiguity with the legacy domain.SignalEvent during
transition. After Step 14 demolition, the legacy one is removed
and trip_ema/domain/__init__.py re-exports this one as the
canonical SignalEvent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


# Allowed values for signal_type. Matches the DB CHECK constraints
# on both current_signals and historical_signals.
SIGNAL_TYPES = ("BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL")

# Allowed values for regime_before and regime_after. Matches DB CHECK.
REGIME_VALUES = (-1, 0, 1)


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """One signal — a regime change at a specific bar for one ticker.

    Field meanings:

    - `ticker_id`, `timeframe`, `bar_timestamp`: identify which
      bar of which ticker series this signal fired on.
    - `signal_type`: one of SIGNAL_TYPES. Derived from
      (regime_before, regime_after) per the BUY/SELL/EXIT mapping.
    - `regime_before`, `regime_after`: display_regime values on
      the previous and current bar respectively. The DB CHECK
      requires regime_before != regime_after (a signal IS a regime
      change by definition).
    - `price_at_signal`: close on `bar_timestamp` (from ohlcv_bars).
      Strictly positive.
    - `computed_at`: when the cycle that detected this signal ran.
    """
    ticker_id: int
    timeframe: str
    bar_timestamp: datetime
    signal_type: str
    regime_before: int
    regime_after: int
    price_at_signal: float
    computed_at: datetime
    signal_date: date | None = None
