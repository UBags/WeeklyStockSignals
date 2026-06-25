"""Tests for TickersRepository."""

from __future__ import annotations

from datetime import datetime, timezone, UTC

import pytest

from trip_ema.domain import TickerRef
from trip_ema.persistence import TickersRepository, transaction


@pytest.fixture
def now():
    return datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


def test_insert_returns_ticker_id(db_engine, now):
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        tid = repo.insert(
            TickerRef("NSE", "NIFTY", "INDEX"),
            full_name="NIFTY 50", created_at=now,
        )
    assert isinstance(tid, int) and tid > 0


def test_get_by_ref_roundtrip(db_engine, now):
    ref = TickerRef("NSE", "RELIANCE", "EQ")
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        tid = repo.insert(ref, full_name="Reliance Industries", created_at=now)

    with db_engine.connect() as conn:
        repo = TickersRepository(conn)
        rec = repo.get_by_ref(ref)

    assert rec is not None
    assert rec.ticker_id == tid
    assert rec.ref == ref
    assert rec.is_active is True
    assert rec.full_name == "Reliance Industries"


def test_get_by_ref_returns_none_when_missing(db_engine):
    with db_engine.connect() as conn:
        repo = TickersRepository(conn)
        assert repo.get_by_ref(TickerRef("NSE", "GHOST", "EQ")) is None


def test_get_by_id_roundtrip(db_engine, now):
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        tid = repo.insert(TickerRef("BSE", "TCS", "EQ"), created_at=now)
    with db_engine.connect() as conn:
        repo = TickersRepository(conn)
        rec = repo.get_by_id(tid)
    assert rec is not None
    assert rec.ref.symbol == "TCS"


def test_find_active_filters_inactive(db_engine, now):
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        repo.insert(TickerRef("NSE", "A", "EQ"), created_at=now)
        repo.insert(TickerRef("NSE", "B", "EQ"), is_active=False, created_at=now)
        repo.insert(TickerRef("NSE", "C", "EQ"), created_at=now)
    with db_engine.connect() as conn:
        repo = TickersRepository(conn)
        active = repo.find_active()
    symbols = sorted(r.ref.symbol for r in active)
    assert symbols == ["A", "C"]


def test_get_ids_for_refs_partial_match(db_engine, now):
    a = TickerRef("NSE", "A", "EQ")
    b = TickerRef("NSE", "B", "EQ")
    missing = TickerRef("NSE", "MISSING", "EQ")
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        repo.insert(a, created_at=now)
        repo.insert(b, created_at=now)
    with db_engine.connect() as conn:
        repo = TickersRepository(conn)
        result = repo.get_ids_for_refs([a, b, missing])
    assert set(result.keys()) == {a, b}


def test_duplicate_ref_insert_raises(db_engine, now):
    """The (exchange, symbol, instrument) unique constraint should catch
    accidental double-inserts."""
    ref = TickerRef("NSE", "DUP", "EQ")
    with transaction(db_engine) as conn:
        TickersRepository(conn).insert(ref, created_at=now)
    with pytest.raises(Exception):  # IntegrityError, but psycopg2 wraps it
        with transaction(db_engine) as conn:
            TickersRepository(conn).insert(ref, created_at=now)
