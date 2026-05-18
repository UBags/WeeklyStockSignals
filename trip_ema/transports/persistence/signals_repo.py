"""Signals repository — write events and read pending events for the
SubscriberDistributor.

`insert_event` is append-only. `find_pending_for_subscriber` is the
centerpiece query used by the SubscriberDistributor to batch undelivered
events, with stale-event suppression: once a newer event exists for the
same (ticker, timeframe), older events are suppressed even if they have
remaining notification budget.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from ..domain import (
    BarState,
    ChangeSignal,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from .tables import (
    notification_log,
    signal_events,
    subscriptions,
    tickers,
)


# Helpers to map between domain enums and DB strings.

_SIGNAL_TO_STR = {
    ChangeSignal.BUY: "BUY",
    ChangeSignal.SELL: "SELL",
    ChangeSignal.EXIT_FROM_BUY: "EXIT_FROM_BUY",
    ChangeSignal.EXIT_FROM_SELL: "EXIT_FROM_SELL",
}
_STR_TO_SIGNAL = {v: k for k, v in _SIGNAL_TO_STR.items()}


def _row_to_event(row, ticker_ref: TickerRef) -> SignalEvent:
    return SignalEvent(
        ticker=ticker_ref,
        timeframe=Timeframe(row.timeframe),
        bar_timestamp=row.bar_timestamp,
        signal=_STR_TO_SIGNAL[row.signal_type],
        price_at_signal=row.price_at_signal,
        computed_at=row.computed_at,
    )


class SignalsRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- signal_events: append-only event log ---------- #

    def insert_event(
        self,
        ticker_id: int,
        event: SignalEvent,
    ) -> int:
        """Append a signal event. Returns the new event_id."""
        if event.signal == ChangeSignal.NO_CHANGE:
            raise ValueError("NO_CHANGE signals should not be inserted as events")
        stmt = (
            pg_insert(signal_events)
            .values(
                ticker_id=ticker_id,
                timeframe=event.timeframe.value,
                bar_timestamp=event.bar_timestamp,
                signal_type=_SIGNAL_TO_STR[event.signal],
                price_at_signal=event.price_at_signal,
                computed_at=event.computed_at,
            )
            .returning(signal_events.c.event_id)
        )
        return self._conn.execute(stmt).scalar_one()

    def recent_events_for_ticker(
        self,
        ticker_id: int,
        ticker_ref: TickerRef,
        *,
        limit: int = 50,
    ) -> list[SignalEvent]:
        """Audit/debug helper: last N events for a ticker."""
        stmt = (
            select(signal_events)
            .where(signal_events.c.ticker_id == ticker_id)
            .order_by(signal_events.c.computed_at.desc())
            .limit(limit)
        )
        return [_row_to_event(r, ticker_ref) for r in self._conn.execute(stmt)]

    # ---------- Notification scheduling: pending events for a subscriber ---------- #

    def find_pending_for_subscriber(
        self,
        subscriber_id: int,
        *,
        max_age: timedelta = timedelta(hours=24),
        max_notifications_each: int = 3,
        now: datetime,
    ) -> list[tuple[int, SignalEvent, TickerRef]]:
        """Return events the subscriber should be notified about.

        Filters applied (in order, all must hold):
          1. Event is within `max_age` of `now`.
          2. Event matches subscriber's subscriptions (ticker + signal filter).
          3. Event is the MOST RECENT for its (ticker_id, timeframe) group.
             Older events for the same ticker/timeframe are suppressed
             entirely, even if they have remaining notification budget.
             (Stale-event suppression: once a newer signal flips the
             ticker, the older one is dead.)
          4. Subscriber has been notified for this event fewer than
             `max_notifications_each` times.

        Returns a list of (event_id, SignalEvent, TickerRef) tuples.
        """
        cutoff = now - max_age

        # Step 1: subscriber's subscriptions
        sub_rows = list(self._conn.execute(
            select(subscriptions.c.ticker_id, subscriptions.c.signal_filter)
            .where(subscriptions.c.subscriber_id == subscriber_id)
        ))
        if not sub_rows:
            return []

        # Build the per-subscription match clauses (same as before).
        clauses = []
        for ticker_id, signal_filter in sub_rows:
            sub_clause = []
            if ticker_id is not None:
                sub_clause.append(signal_events.c.ticker_id == ticker_id)
            if signal_filter is not None:
                allowed = [s.strip() for s in signal_filter.split(",") if s.strip()]
                if allowed:
                    sub_clause.append(signal_events.c.signal_type.in_(allowed))
            clauses.append(and_(*sub_clause) if sub_clause else True)
        match_any_subscription = or_(*clauses)

        # Step 2: count prior notifications per event for this subscriber.
        count_subq = (
            select(
                notification_log.c.event_id,
                func.count().label("send_count"),
            )
            .where(notification_log.c.subscriber_id == subscriber_id)
            .group_by(notification_log.c.event_id)
            .subquery()
        )

        # Step 3: pick the MOST RECENT event per (ticker_id, timeframe)
        # within the time window. Postgres-specific DISTINCT ON; simpler
        # and faster than ROW_NUMBER() for this case.
        #
        # We use raw SQL here because SQLAlchemy Core's `select()` doesn't
        # express DISTINCT ON cleanly without significant boilerplate, and
        # the resulting subquery is straightforward.
        latest_per_ticker_sql = text("""
            SELECT DISTINCT ON (ticker_id, timeframe)
                event_id, ticker_id, timeframe, bar_timestamp,
                signal_type, price_at_signal, computed_at
            FROM signal_events
            WHERE computed_at >= :cutoff
            ORDER BY ticker_id, timeframe, computed_at DESC
        """)
        latest_subq = (
            self._conn.execute(latest_per_ticker_sql, {"cutoff": cutoff})
            .mappings()
            .all()
        )
        if not latest_subq:
            return []

        # Step 4: filter the latest events by subscription match + cap.
        latest_event_ids = [r["event_id"] for r in latest_subq]

        stmt = (
            select(signal_events, tickers, count_subq.c.send_count)
            .join(tickers, tickers.c.ticker_id == signal_events.c.ticker_id)
            .join(
                count_subq,
                count_subq.c.event_id == signal_events.c.event_id,
                isouter=True,
            )
            .where(
                signal_events.c.event_id.in_(latest_event_ids),
                match_any_subscription,
                or_(
                    count_subq.c.send_count.is_(None),
                    count_subq.c.send_count < max_notifications_each,
                ),
            )
            .order_by(signal_events.c.computed_at.asc())
        )

        out: list[tuple[int, SignalEvent, TickerRef]] = []
        for row in self._conn.execute(stmt):
            ref = TickerRef(
                exchange=row.exchange, symbol=row.symbol, instrument=row.instrument,
            )
            event = _row_to_event(row, ref)
            out.append((row.event_id, event, ref))
        return out

    def is_event_superseded(self, event_id: int) -> bool:
        """NEW 3.4 (Phase 9): Return True if a strictly-newer event exists
        for the same (ticker_id, timeframe) as the event identified by
        `event_id`. False otherwise (including when `event_id` doesn't
        exist — defensive).

        Used by NotifierWorker as a last-moment freshness check before
        actually dispatching a notification: if a BUY job has been
        sitting in the Redis queue while an EXIT was generated and added
        to signal_events, this lets the worker drop the BUY without
        sending the stale alert.

        The query is two parts:
          (a) read the (ticker_id, timeframe, computed_at) of the target
          (b) check whether any row with the same (ticker_id, timeframe)
              but strictly later computed_at exists

        Implemented as one SQL statement via a correlated subquery.
        """
        sql = text("""
            SELECT EXISTS (
                SELECT 1
                FROM signal_events newer
                INNER JOIN signal_events target
                    ON target.event_id = :event_id
                WHERE newer.ticker_id = target.ticker_id
                  AND newer.timeframe = target.timeframe
                  AND newer.computed_at > target.computed_at
            )
        """)
        result = self._conn.execute(sql, {"event_id": event_id}).scalar()
        return bool(result)
