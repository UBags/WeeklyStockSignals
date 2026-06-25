"""Shared I/O layer for signal events.

Wraps `SignalsRepository.insert_event` with the per-event loop,
optional signal-cache write-through, and an explicit `computed_at`
policy contract. Both signals/generate_and_backfill_signals.py
(historical batch replay) and signals/generate_signals_in_live_-
production.py (live ticks) call into this single function.

The difference between backfill and live is purely the `computed_at`
each writer stamps on the SignalEvents it constructs:

  - **Backfill**: `computed_at` = the simulated day being walked
    (forward-walk replay).
  - **Live**: `computed_at` = NOW().

This file does not own that policy — the writer does. We accept
whatever `computed_at` is on the event and persist it verbatim.

What this file does own:
  - The actual INSERT (delegated to `SignalsRepository.insert_event`,
    which uses ON CONFLICT DO NOTHING on the partial unique index
    from migration 0016).
  - The per-event iteration.
  - The optional signal-cache write-through. Pass None during batch
    replay to avoid cache churn during a multi-hour walk; the cache
    is rebuilt at end-of-run via the lifecycle path.
  - The guardrail that NO_CHANGE events are never persisted.

What this file deliberately does NOT do:
  - Compute regimes or signals. That's the pure path in
    `regime/compute_regime.py` and `signals/generate_signals.py`.
  - Decide the `computed_at` policy. That's the writer.
  - Open or close transactions. The caller passes a `Connection`
    that's already inside whatever transaction shape they want.
  - Handle industry-level signal events. The current `SignalEvent`
    domain type models per-ticker signals only; industry support
    arrives alongside the writer's industry path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy.engine import Connection

from trip_ema.domain import ChangeSignal, SignalEvent, Timeframe
from trip_ema.persistence.signals_repo import SignalsRepository


# Type alias for the optional signal-cache write-through callback.
#
# Step 8 repurposes the existing cache/signal_cache.SignalCache to
# hold "latest event per (ticker_id, timeframe)" snapshots. Until
# Step 8 lands, this type is just the callable shape: the live
# orchestrator passes a real writer in production; backfill and
# tests pass None.
SignalCacheWriter = Callable[[int, Timeframe, SignalEvent], None]


def persist_signal_events(
    conn: Connection,
    ticker_id: int,
    events: Iterable[SignalEvent],
    *,
    signal_cache_writer: SignalCacheWriter | None = None,
) -> list[int]:
    """Persist a sequence of SignalEvents to the `signal_events` table.

    Returns the list of `event_id`s in input order. For idempotent
    re-inserts (same (ticker_id, timeframe, bar_timestamp, signal_type)
    as an existing row), the returned `event_id` is the existing row's
    — no new row is created.

    Args:
        conn: SQLAlchemy `Connection` already inside the caller's
            transaction. We do not commit, roll back, or open nested
            transactions. The caller controls the boundary.
        ticker_id: FK into `tickers`. The writer resolves this from
            the `TickerRef` it's iterating; we accept the integer
            directly to keep this layer free of `tickers` lookups.
        events: SignalEvents to write. Each event's `computed_at`
            field is honoured verbatim — this is how the
            backfill-vs-live policy is expressed. Each event's
            `timeframe` should match the writer's current series.
        signal_cache_writer: If provided, invoked AFTER each
            successful insert with `(ticker_id, event.timeframe,
            event)`. The callback is responsible for cache mechanics
            (locking, snapshot replacement). Pass None to skip the
            cache update entirely (recommended during multi-hour
            batch replays).

    Raises:
        ValueError: If any event has `signal == ChangeSignal.NO_CHANGE`.
            NO_CHANGE means "no signal fired" and must be filtered
            at the writer; persisting one would be a violation of the
            invariant that every row in `signal_events` represents an
            actual trading-state transition.

    Notes:
        Idempotency is provided by migration 0016's partial unique
        index on `(ticker_id, timeframe, bar_timestamp, signal_type)`.
        The repo's `insert_event` does the conflict handling and the
        fallback SELECT in one round-trip in the common case.

        Order preservation in the return value: if you pass three
        events, you get back three event_ids in the same order. This
        matters when the writer threads event_ids back into its own
        bookkeeping (e.g., progress checkpointing in the backfill).
    """
    repo = SignalsRepository(conn)
    event_ids: list[int] = []
    for event in events:
        if event.signal == ChangeSignal.NO_CHANGE:
            raise ValueError(
                "Cannot persist a NO_CHANGE event. Filter NO_CHANGE "
                "at the writer before calling persist_signal_events."
            )
        event_id = repo.insert_event(ticker_id, event)
        event_ids.append(event_id)
        if signal_cache_writer is not None:
            signal_cache_writer(ticker_id, event.timeframe, event)
    return event_ids
