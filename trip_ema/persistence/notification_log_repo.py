"""V1 NotificationLogRepository — writes the denormalized columns.

After migration 0027, `notification_log` carries the signal identity
inline (no FK to signal_events). This repo is the V1 writer that
populates the new columns.

Coexistence note:
  - The legacy `SubscribersRepository.record_notification(event_id=...)`
    still works because we did NOT drop `event_id` in Step 8 — that
    happens in Step 14 alongside the demolition of the legacy notifier
    worker.
  - This repo writes ONLY the new V1 columns (and the always-present
    subscriber_id + last_sent_at + notification_count). It does NOT
    populate event_id; rows written by this repo will have
    event_id = NULL.
  - Step 14 enforces NOT NULL on the V1 columns and drops event_id.

Partitioning note:
  `notification_log` is partitioned by `last_sent_at`. The composite PK
  is `(log_id, last_sent_at)` because the partition key must be in the
  PK. Our INSERT supplies last_sent_at explicitly; Postgres picks the
  right partition.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True, slots=True)
class NotificationLogEntry:
    """One signal-subscriber notification record. Mirrors the V1
    columns of notification_log; does NOT carry log_id (DB-assigned)
    or event_id (legacy, NULL for V1 writes)."""
    subscriber_id: int
    signal_ticker_id: int
    signal_timeframe: str
    signal_bar_timestamp: datetime
    signal_type: str
    signal_price: float
    sent_at: datetime


_VALID_SIGNAL_TYPES = frozenset({
    "BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL",
})


class NotificationLogRepository:
    """V1 writer for `notification_log`. Each call records ONE
    notification (one subscriber × one signal). Step 9's
    notify_subscribers batches the writes — N signals × M subscribers
    = N×M rows per cycle (when each subscriber receives all signals).
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- writes ---------- #

    def record(self, entry: NotificationLogEntry) -> None:
        """Append one notification row. Idempotency is the caller's
        problem — there's no UNIQUE constraint pinning (subscriber_id,
        ticker_id, signal_type, bar_timestamp) at the schema level, so
        duplicate calls produce duplicate rows. Step 9's flow ensures
        one call per subscriber-per-signal-per-cycle."""
        self._validate(entry)
        self._conn.execute(
            text("""
                INSERT INTO notification_log (
                    subscriber_id,
                    signal_ticker_id,
                    signal_timeframe,
                    signal_bar_timestamp,
                    signal_type,
                    signal_price,
                    notification_count,
                    last_sent_at
                ) VALUES (
                    :sid, :tid, :tf, :bt, :st, :sp, 1, :sent
                )
            """),
            self._params(entry),
        )

    def record_many(self, entries: list[NotificationLogEntry]) -> int:
        """Batch insert. Returns the row count written. Useful when
        one subscriber gets many signals — fewer round trips."""
        if not entries:
            return 0
        for entry in entries:
            self._validate(entry)
        self._conn.execute(
            text("""
                INSERT INTO notification_log (
                    subscriber_id,
                    signal_ticker_id,
                    signal_timeframe,
                    signal_bar_timestamp,
                    signal_type,
                    signal_price,
                    notification_count,
                    last_sent_at
                ) VALUES (
                    :sid, :tid, :tf, :bt, :st, :sp, 1, :sent
                )
            """),
            [self._params(e) for e in entries],
        )
        return len(entries)

    # ---------- reads ---------- #

    def count_for_subscriber(self, subscriber_id: int) -> int:
        """Total notifications sent to this subscriber (all time, all
        signals). Used by admin views; cheap because of the partition
        + the subscriber_id partial index."""
        return self._conn.execute(
            text("""
                SELECT COUNT(*) FROM notification_log
                 WHERE subscriber_id = :sid
            """),
            {"sid": subscriber_id},
        ).scalar_one()

    def count_v1(self) -> int:
        """Count of rows written by the V1 writer (signal_ticker_id
        IS NOT NULL). Mainly for tests / migration verification."""
        return self._conn.execute(
            text("""
                SELECT COUNT(*) FROM notification_log
                 WHERE signal_ticker_id IS NOT NULL
            """),
        ).scalar_one()

    # ---------- internals ---------- #

    @staticmethod
    def _validate(entry: NotificationLogEntry) -> None:
        if entry.signal_type not in _VALID_SIGNAL_TYPES:
            raise ValueError(
                f"signal_type must be in {sorted(_VALID_SIGNAL_TYPES)}, "
                f"got {entry.signal_type!r}"
            )
        if entry.signal_price <= 0:
            # Not enforced by DB CHECK (we kept the column nullable
            # for transition) but defensive at the app layer — a
            # zero/negative price is a degenerate signal.
            raise ValueError(
                f"signal_price must be > 0, got {entry.signal_price}"
            )

    @staticmethod
    def _params(entry: NotificationLogEntry) -> dict:
        return {
            "sid": entry.subscriber_id,
            "tid": entry.signal_ticker_id,
            "tf": entry.signal_timeframe,
            "bt": entry.signal_bar_timestamp,
            "st": entry.signal_type,
            "sp": entry.signal_price,
            "sent": entry.sent_at,
        }
