"""Tests for SubscribersRepository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import pytest

from trip_ema.persistence import SubscribersRepository, transaction


@pytest.fixture
def now():
    return datetime.now(UTC).replace(microsecond=0)


# ---------- Subscribers ---------- #

def test_create_and_get_subscriber(db_engine, now):
    with transaction(db_engine) as conn:
        repo = SubscribersRepository(conn)
        sid = repo.create_subscriber(
            transport="email", address="user@example.com",
            notify_cadence_minutes=60, created_at=now,
        )
    with db_engine.connect() as conn:
        rec = SubscribersRepository(conn).get(sid)
    assert rec is not None
    assert rec.subscriber_id == sid
    assert rec.transport == "email"
    assert rec.notify_cadence_minutes == 60
    assert rec.is_active is True
    assert rec.last_notified_at is None


def test_invalid_cadence_rejected(db_engine, now):
    """The CHECK constraint should reject a cadence that's not a multiple
    of 15 or outside [15, 1440]."""
    with pytest.raises(Exception):
        with transaction(db_engine) as conn:
            SubscribersRepository(conn).create_subscriber(
                transport="email", address="x", notify_cadence_minutes=20,  # not a multiple of 15
                created_at=now,
            )


def test_invalid_transport_rejected(db_engine, now):
    with pytest.raises(Exception), transaction(db_engine) as conn:
        SubscribersRepository(conn).create_subscriber(
            transport="carrier_pigeon", address="x", notify_cadence_minutes=15,
            created_at=now,
        )


# ---------- find_due_for_notification ---------- #

def test_never_notified_subscriber_is_due(db_engine, now):
    with transaction(db_engine) as conn:
        sid = SubscribersRepository(conn).create_subscriber(
            transport="email", address="x", notify_cadence_minutes=60,
            created_at=now,
        )
    with db_engine.connect() as conn:
        due = SubscribersRepository(conn).find_due_for_notification(now)
    assert [d.subscriber_id for d in due] == [sid]


def test_recently_notified_subscriber_not_due(db_engine, now):
    with transaction(db_engine) as conn:
        repo = SubscribersRepository(conn)
        sid = repo.create_subscriber(
            transport="email", address="x", notify_cadence_minutes=60,
            created_at=now - timedelta(hours=10),
        )
        repo.update_last_notified(sid, now - timedelta(minutes=30))
    # 30 minutes since last notification, cadence is 60 → not yet due
    with db_engine.connect() as conn:
        due = SubscribersRepository(conn).find_due_for_notification(now)
    assert due == []


def test_subscriber_due_after_cadence_elapsed(db_engine, now):
    with transaction(db_engine) as conn:
        repo = SubscribersRepository(conn)
        sid = repo.create_subscriber(
            transport="email", address="x", notify_cadence_minutes=60,
            created_at=now - timedelta(hours=2),
        )
        repo.update_last_notified(sid, now - timedelta(minutes=61))
    with db_engine.connect() as conn:
        due = SubscribersRepository(conn).find_due_for_notification(now)
    assert [d.subscriber_id for d in due] == [sid]


def test_inactive_subscriber_never_due(db_engine, now):
    with transaction(db_engine) as conn:
        repo = SubscribersRepository(conn)
        sid = repo.create_subscriber(
            transport="email", address="x", notify_cadence_minutes=15,
            created_at=now - timedelta(days=1),
        )
        repo.deactivate(sid)
    with db_engine.connect() as conn:
        due = SubscribersRepository(conn).find_due_for_notification(now)
    assert due == []


# ---------- Subscriptions ---------- #

def test_add_and_list_subscriptions(db_engine, now):
    with transaction(db_engine) as conn:
        subs = SubscribersRepository(conn)
        sid = subs.create_subscriber(
            transport="email", address="x", notify_cadence_minutes=15,
            created_at=now,
        )
        subs.add_subscription(
            subscriber_id=sid, ticker_id=None, signal_filter=None, created_at=now,
        )
        subs.add_subscription(
            subscriber_id=sid, ticker_id=None, signal_filter="BUY,SELL", created_at=now,
        )
    with db_engine.connect() as conn:
        listed = SubscribersRepository(conn).subscriptions_for(sid)
    assert len(listed) == 2
    filters = sorted(s.signal_filter or "" for s in listed)
    assert filters == ["", "BUY,SELL"]


# ---------- Notification log ---------- #

# === STEP 15-V2-FIXUP2: test_record_notification_creates_row removed ===
# It exercised the legacy notification_log.event_id FK to
# signal_events.event_id — both demolished in Step 15. V1 uses
# NotificationLogRepository (denormalized columns, no FK) via
# orchestrator/notify_subscribers.py.
