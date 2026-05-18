"""Subscribers repository — subscriber registry, subscription filters,
and notification audit log writes.

Three responsibilities:
  - Read/write subscribers (`subscribers` table)
  - Manage their subscriptions (`subscriptions` table)
  - Append to the notification log (`notification_log` table) — never reads;
    the SignalsRepository handles the "have I sent this before" join.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.engine import Connection

from .tables import notification_log, subscribers, subscriptions


@dataclass(frozen=True, slots=True)
class SubscriberRecord:
    subscriber_id: int
    transport: str
    address: str
    notify_cadence_minutes: int
    last_notified_at: datetime | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class SubscriptionRecord:
    subscription_id: int
    subscriber_id: int
    ticker_id: int | None       # None == all tickers
    signal_filter: str | None   # None == all signals


def _row_to_subscriber(row) -> SubscriberRecord:
    return SubscriberRecord(
        subscriber_id=row.subscriber_id,
        transport=row.transport,
        address=row.address,
        notify_cadence_minutes=row.notify_cadence_minutes,
        last_notified_at=row.last_notified_at,
        is_active=row.is_active,
    )


def _row_to_subscription(row) -> SubscriptionRecord:
    return SubscriptionRecord(
        subscription_id=row.subscription_id,
        subscriber_id=row.subscriber_id,
        ticker_id=row.ticker_id,
        signal_filter=row.signal_filter,
    )


class SubscribersRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- Subscribers ---------- #

    def create_subscriber(
        self,
        *,
        transport: str,
        address: str,
        notify_cadence_minutes: int,
        created_at: datetime,
    ) -> int:
        """Insert a subscriber. Returns the new subscriber_id."""
        stmt = (
            insert(subscribers)
            .values(
                transport=transport,
                address=address,
                notify_cadence_minutes=notify_cadence_minutes,
                last_notified_at=None,
                is_active=True,
                created_at=created_at,
            )
            .returning(subscribers.c.subscriber_id)
        )
        return self._conn.execute(stmt).scalar_one()

    def get(self, subscriber_id: int) -> SubscriberRecord | None:
        stmt = select(subscribers).where(subscribers.c.subscriber_id == subscriber_id)
        row = self._conn.execute(stmt).first()
        return _row_to_subscriber(row) if row else None

    def find_due_for_notification(self, now: datetime) -> list[SubscriberRecord]:
        """Return active subscribers whose cadence has elapsed since their
        last_notified_at (or who've never been notified).

        Filter is applied in SQL using PostgreSQL's `make_interval()`
        function so the work happens at the database. With the partial
        index `idx_subscribers_active_due ON subscribers(last_notified_at)
        WHERE is_active = TRUE`, this becomes an indexed scan over the
        active subset, returning only the rows that are genuinely due —
        no Python-side filtering over the full active subscriber list.

        `now` is passed in as a parameter (rather than relying on the DB
        clock) for deterministic tests and consistent semantics across
        multiple workers.
        """
        # make_interval(years, months, weeks, days, hours, mins) takes
        # column references safely without string interpolation. We use
        # the `mins` slot with the per-row cadence value.
        cadence_interval = func.make_interval(
            0, 0, 0, 0, 0,
            subscribers.c.notify_cadence_minutes,
        )
        stmt = (
            select(subscribers)
            .where(
                subscribers.c.is_active.is_(True),
                or_(
                    subscribers.c.last_notified_at.is_(None),
                    subscribers.c.last_notified_at <= now - cadence_interval,
                ),
            )
        )
        return [_row_to_subscriber(r) for r in self._conn.execute(stmt)]

    def update_last_notified(self, subscriber_id: int, at: datetime) -> None:
        stmt = (
            update(subscribers)
            .where(subscribers.c.subscriber_id == subscriber_id)
            .values(last_notified_at=at)
        )
        self._conn.execute(stmt)

    def deactivate(self, subscriber_id: int) -> None:
        stmt = (
            update(subscribers)
            .where(subscribers.c.subscriber_id == subscriber_id)
            .values(is_active=False)
        )
        self._conn.execute(stmt)

    # ---------- Subscriptions ---------- #

    def add_subscription(
        self,
        *,
        subscriber_id: int,
        ticker_id: int | None,
        signal_filter: str | None,
        created_at: datetime,
    ) -> int:
        stmt = (
            insert(subscriptions)
            .values(
                subscriber_id=subscriber_id,
                ticker_id=ticker_id,
                signal_filter=signal_filter,
                created_at=created_at,
            )
            .returning(subscriptions.c.subscription_id)
        )
        return self._conn.execute(stmt).scalar_one()

    def subscriptions_for(self, subscriber_id: int) -> list[SubscriptionRecord]:
        stmt = select(subscriptions).where(subscriptions.c.subscriber_id == subscriber_id)
        return [_row_to_subscription(r) for r in self._conn.execute(stmt)]

    # ---------- Notification log ---------- #

    def record_notification(
        self,
        *,
        subscriber_id: int,
        event_id: int,
        sent_at: datetime,
    ) -> None:
        """Append a notification-sent row. Each call counts as one delivery;
        the 'how many times has this subscriber been notified about this
        event' tally comes from counting rows in this table."""
        stmt = insert(notification_log).values(
            subscriber_id=subscriber_id,
            event_id=event_id,
            notification_count=1,
            last_sent_at=sent_at,
        )
        self._conn.execute(stmt)

    def record_batch(
        self,
        *,
        subscriber_id: int,
        event_ids: Iterable[int],
        sent_at: datetime,
    ) -> None:
        """Convenience: record one notification per event in a batch."""
        for event_id in event_ids:
            self.record_notification(
                subscriber_id=subscriber_id,
                event_id=event_id,
                sent_at=sent_at,
            )
