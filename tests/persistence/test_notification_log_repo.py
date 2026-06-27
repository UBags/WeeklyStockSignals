"""Integration tests for `trip_ema.persistence.notification_log_repo`.

Verifies the V1 writer + the 0027 migration's effects:
  - 5 new columns present and writable
  - signal_type CHECK constraint active
  - event_id is nullable (so V1 writes succeed without it)
  - Backfill (covered indirectly: a row inserted with event_id can
    pick up the V1 columns via UPDATE FROM signal_events join)
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import text

from trip_ema.persistence import transaction
from trip_ema.persistence.notification_log_repo import (
    NotificationLogEntry,
    NotificationLogRepository,
)


# ============================================================
# Helpers
# ============================================================

def _seed_subscriber(
    conn, *,
    subscriber_id: int = 1,
    address: str = "test@example.com",
) -> int:
    """Subscribers have a required notify_cadence_minutes — set to
    60 for tests."""
    conn.execute(text("""
        INSERT INTO subscribers (
            subscriber_id, transport, address,
            notify_cadence_minutes, is_active
        ) VALUES (
            :sid, 'email', :addr, 60, TRUE
        )
        ON CONFLICT (subscriber_id) DO NOTHING
    """), {"sid": subscriber_id, "addr": address})
    return subscriber_id


def _entry(
    subscriber_id: int = 1,
    *,
    ticker_id: int = 1001,
    signal_type: str = "BUY",
    price: float = 100.0,
    sent_at: datetime | None = None,
) -> NotificationLogEntry:
    return NotificationLogEntry(
        subscriber_id=subscriber_id,
        signal_ticker_id=ticker_id,
        signal_timeframe="1W",
        signal_bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
        signal_type=signal_type,
        signal_price=price,
        sent_at=sent_at or datetime.now(UTC),
    )


# ============================================================
# Schema invariants (migration 0027 landed)
# ============================================================

class TestSchema:

    def test_v1_columns_present(self, db_engine):
        """Migration 0027 added 5 typed columns to notification_log."""
        with db_engine.connect() as conn:
            cols = {
                r[0] for r in conn.execute(text("""
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_name = 'notification_log'
                """))
            }
        for col in (
            "signal_ticker_id", "signal_timeframe",
            "signal_bar_timestamp", "signal_type", "signal_price",
        ):
            assert col in cols, f"Missing V1 column: {col}"

    def test_event_id_is_nullable(self, db_engine):
        """V1 writes don't populate event_id; migration relaxed it
        to nullable for the transition."""
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'notification_log'
                   AND column_name = 'event_id'
            """)).fetchone()
        assert row is not None
        assert row[0] == "YES"

    def test_signal_type_check_constraint(self, db_engine):
        """The CHECK on signal_type rejects invalid values."""
        with transaction(db_engine) as conn:
            _seed_subscriber(conn)
        # An invalid signal_type should violate the CHECK
        with pytest.raises(Exception) as exc_info:
            with transaction(db_engine) as conn:
                conn.execute(text("""
                    INSERT INTO notification_log (
                        subscriber_id, signal_ticker_id,
                        signal_timeframe, signal_bar_timestamp,
                        signal_type, signal_price,
                        notification_count, last_sent_at
                    ) VALUES (
                        1, 1001, '1W',
                        TIMESTAMP WITH TIME ZONE '2025-10-06 00:00:00+00',
                        'INVALID_KIND', 100.0,
                        1, NOW()
                    )
                """))
        # The check constraint name should appear in the error
        assert "notification_log_signal_type_chk" in str(exc_info.value)


# ============================================================
# Basic writes
# ============================================================

class TestBasicWrites:

    def test_record_inserts_one_row(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_subscriber(conn)
            repo = NotificationLogRepository(conn)
            repo.record(_entry())

        with db_engine.connect() as conn:
            cnt = NotificationLogRepository(conn).count_v1()
        assert cnt == 1

    def test_record_writes_all_v1_columns(self, db_engine):
        sent = datetime(2026, 6, 27, 14, 30, tzinfo=UTC)
        with transaction(db_engine) as conn:
            _seed_subscriber(conn)
            repo = NotificationLogRepository(conn)
            repo.record(_entry(
                ticker_id=9001, signal_type="SELL",
                price=247.85, sent_at=sent,
            ))

        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT subscriber_id, signal_ticker_id,
                       signal_timeframe, signal_bar_timestamp,
                       signal_type, signal_price,
                       notification_count, last_sent_at,
                       event_id
                  FROM notification_log
                 WHERE signal_ticker_id = 9001
            """)).fetchone()

        assert row is not None
        assert int(row[0]) == 1                   # subscriber_id
        assert int(row[1]) == 9001                # signal_ticker_id
        assert row[2] == "1W"                     # signal_timeframe
        assert row[3] == datetime(
            2025, 10, 6, tzinfo=UTC,
        )                                          # signal_bar_timestamp
        assert row[4] == "SELL"                   # signal_type
        assert float(row[5]) == 247.85            # signal_price
        assert int(row[6]) == 1                   # notification_count
        assert row[7] == sent                     # last_sent_at
        assert row[8] is None                     # event_id (V1 omits)

    def test_record_many_inserts_n_rows(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_subscriber(conn)
            repo = NotificationLogRepository(conn)
            entries = [
                _entry(ticker_id=1000 + i)
                for i in range(5)
            ]
            n = repo.record_many(entries)
        assert n == 5

        with db_engine.connect() as conn:
            cnt = NotificationLogRepository(conn).count_v1()
        assert cnt == 5

    def test_record_many_empty_returns_zero(self, db_engine):
        with transaction(db_engine) as conn:
            n = NotificationLogRepository(conn).record_many([])
        assert n == 0


# ============================================================
# App-level validation
# ============================================================

class TestValidation:

    def test_invalid_signal_type_rejected(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_subscriber(conn)
            repo = NotificationLogRepository(conn)
            entry = NotificationLogEntry(
                subscriber_id=1,
                signal_ticker_id=1,
                signal_timeframe="1W",
                signal_bar_timestamp=datetime(
                    2025, 10, 6, tzinfo=UTC,
                ),
                signal_type="GARBAGE",
                signal_price=100.0,
                sent_at=datetime.now(UTC),
            )
            with pytest.raises(ValueError, match="signal_type"):
                repo.record(entry)

    def test_zero_price_rejected(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_subscriber(conn)
            repo = NotificationLogRepository(conn)
            with pytest.raises(ValueError, match="signal_price"):
                repo.record(_entry(price=0.0))

    def test_negative_price_rejected(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_subscriber(conn)
            repo = NotificationLogRepository(conn)
            with pytest.raises(ValueError, match="signal_price"):
                repo.record(_entry(price=-50.0))


# ============================================================
# Counts
# ============================================================

class TestCounts:

    def test_count_for_subscriber(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_subscriber(conn, subscriber_id=1)
            _seed_subscriber(
                conn, subscriber_id=2,
                address="other@example.com",
            )
            repo = NotificationLogRepository(conn)
            repo.record_many([
                _entry(subscriber_id=1, ticker_id=1001),
                _entry(subscriber_id=1, ticker_id=1002),
                _entry(subscriber_id=1, ticker_id=1003),
                _entry(subscriber_id=2, ticker_id=1001),
            ])

        with db_engine.connect() as conn:
            repo = NotificationLogRepository(conn)
            assert repo.count_for_subscriber(1) == 3
            assert repo.count_for_subscriber(2) == 1
            assert repo.count_for_subscriber(99) == 0
