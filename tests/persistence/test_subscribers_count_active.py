"""Tests for SubscribersRepository.count_active (Step 13d-1).

Used by the Run Intraday confirmation modal to surface "this will
email N subscribers" before the operator confirms the cycle.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from trip_ema.persistence import transaction
from trip_ema.persistence.subscribers_repo import SubscribersRepository


def _seed_subscriber(
    conn,
    *,
    address: str,
    is_active: bool = True,
    transport: str = "email",
    notify_cadence_minutes: int = 60,
) -> int:
    row = conn.execute(text("""
        INSERT INTO subscribers (
            transport, address, notify_cadence_minutes, is_active
        ) VALUES (
            :transport, :address, :cadence, :active
        )
        RETURNING subscriber_id
    """), {
        "transport": transport, "address": address,
        "cadence": notify_cadence_minutes,
        "active": is_active,
    }).first()
    return int(row.subscriber_id)


class TestCountActive:

    def test_zero_when_empty(self, db_engine):
        with db_engine.connect() as conn:
            n = SubscribersRepository(conn).count_active()
        assert n == 0

    def test_counts_only_active(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_subscriber(conn, address="a@x.com")
            _seed_subscriber(conn, address="b@x.com")
            _seed_subscriber(
                conn, address="c@x.com", is_active=False,
            )
        with db_engine.connect() as conn:
            assert SubscribersRepository(
                conn,
            ).count_active() == 2

    def test_returns_int(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_subscriber(conn, address="a@x.com")
        with db_engine.connect() as conn:
            n = SubscribersRepository(conn).count_active()
        assert isinstance(n, int)

    def test_grows_with_inserts(self, db_engine):
        with db_engine.connect() as conn:
            assert SubscribersRepository(
                conn,
            ).count_active() == 0
        with transaction(db_engine) as conn:
            _seed_subscriber(conn, address="a@x.com")
        with db_engine.connect() as conn:
            assert SubscribersRepository(
                conn,
            ).count_active() == 1
        with transaction(db_engine) as conn:
            _seed_subscriber(conn, address="b@x.com")
            _seed_subscriber(conn, address="c@x.com")
        with db_engine.connect() as conn:
            assert SubscribersRepository(
                conn,
            ).count_active() == 3

    def test_reactivation_increments_count(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_subscriber(
                conn, address="a@x.com", is_active=False,
            )
        with db_engine.connect() as conn:
            assert SubscribersRepository(
                conn,
            ).count_active() == 0
        # Re-activate
        with transaction(db_engine) as conn:
            conn.execute(text(
                "UPDATE subscribers SET is_active = TRUE "
                "WHERE address = 'a@x.com'"
            ))
        with db_engine.connect() as conn:
            assert SubscribersRepository(
                conn,
            ).count_active() == 1
