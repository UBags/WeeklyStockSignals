"""Tests for ExchangeStateRepository (Phase 14.6b).

Uses the shared `db_engine` fixture in this directory's conftest.py
— a fresh, migrated Postgres database per test. The migration
seeds an NSE row, so most tests use a non-NSE exchange (BSE, NYSE)
to avoid interference, and a dedicated test asserts the seeded
NSE row exists in its expected shape.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, UTC

import pytest
from sqlalchemy import text

from trip_ema.persistence.exchange_state_repo import (
    ExchangeStateRepository,
)


# ---------- seed row from the 0004 migration ---------- #


class TestNseSeedRow:
    """The 0004 migration inserts an NSE row with values captured
    from the live production DB at design time. These tests assert
    the seed is present and uses the exact values."""

    def test_nse_row_exists_after_migration(self, db_engine):
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("NSE")
        assert row is not None
        assert row.exchange == "NSE"

    def test_nse_seed_initial_load_started(self, db_engine):
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("NSE")
        assert row.initial_load_started_at is not None
        # Stored value: '2026-05-18 22:52:37+05:30' = UTC '2026-05-18 17:22:37'
        assert row.initial_load_started_at.astimezone(UTC) == datetime(
            2026, 5, 18, 17, 22, 37, tzinfo=UTC,
        )

    def test_nse_seed_initial_load_completed(self, db_engine):
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("NSE")
        assert row.initial_load_completed_at is not None
        # Stored: '2026-05-19 02:09:23+05:30' = UTC '2026-05-18 20:39:23'
        assert row.initial_load_completed_at.astimezone(UTC) == datetime(
            2026, 5, 18, 20, 39, 23, tzinfo=UTC,
        )

    def test_nse_seed_last_eod_fields(self, db_engine):
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("NSE")
        assert row.last_eod_outcome == "success"
        assert row.last_eod_data_date == date(2026, 5, 19)
        assert row.last_eod_run_id is None  # not retroactively known

    def test_only_nse_seeded(self, db_engine):
        """Other exchanges should NOT have rows just because their
        codes exist in the suffix map. Configuration ≠ activity."""
        with db_engine.connect() as conn:
            assert ExchangeStateRepository(conn).get("BSE") is None
            assert ExchangeStateRepository(conn).get("NYSE") is None


# ---------- list_all ---------- #


class TestListAll:

    def test_empty_when_only_nse_seed(self, db_engine):
        """Only the seeded NSE row exists immediately after migration."""
        with db_engine.connect() as conn:
            rows = ExchangeStateRepository(conn).list_all()
        assert [r.exchange for r in rows] == ["NSE"]

    def test_ordered_by_exchange_code(self, db_engine):
        with db_engine.begin() as conn:
            repo = ExchangeStateRepository(conn)
            now = datetime.now(UTC)
            repo.record_intraday("NYSE", run_at=now)
            repo.record_intraday("BSE", run_at=now)
        with db_engine.connect() as conn:
            rows = ExchangeStateRepository(conn).list_all()
        # Alphabetic: BSE, NSE, NYSE
        assert [r.exchange for r in rows] == ["BSE", "NSE", "NYSE"]


# ---------- record_eod ---------- #


class TestRecordEod:

    def test_creates_row_for_new_exchange(self, db_engine):
        now = datetime(2026, 5, 21, 1, 0, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            ExchangeStateRepository(conn).record_eod(
                "BSE", run_at=now, outcome="success",
                data_date=date(2026, 5, 20), run_id=42,
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row is not None
        assert row.last_eod_run_at == now
        assert row.last_eod_outcome == "success"
        assert row.last_eod_data_date == date(2026, 5, 20)
        assert row.last_eod_run_id == 42

    def test_updates_existing_row(self, db_engine):
        """Calling record_eod twice updates the same row, not creates two."""
        ts1 = datetime(2026, 5, 21, 1, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 5, 22, 1, 0, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            repo = ExchangeStateRepository(conn)
            repo.record_eod("BSE", run_at=ts1, outcome="success",
                            data_date=date(2026, 5, 20), run_id=1)
            repo.record_eod("BSE", run_at=ts2, outcome="partial",
                            data_date=date(2026, 5, 21), run_id=2)
        with db_engine.connect() as conn:
            rows = ExchangeStateRepository(conn).list_all()
            bse = ExchangeStateRepository(conn).get("BSE")
        # Only one BSE row
        assert sum(1 for r in rows if r.exchange == "BSE") == 1
        # Latest wins
        assert bse.last_eod_run_at == ts2
        assert bse.last_eod_outcome == "partial"
        assert bse.last_eod_data_date == date(2026, 5, 21)
        assert bse.last_eod_run_id == 2

    def test_failed_outcome_persists(self, db_engine):
        """A failed EoD still updates state — operators need to see it."""
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            ExchangeStateRepository(conn).record_eod(
                "BSE", run_at=now, outcome="failed",
                data_date=None, run_id=99,
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.last_eod_outcome == "failed"
        assert row.last_eod_data_date is None

    def test_invalid_outcome_rejected_by_check_constraint(self, db_engine):
        """The CHECK constraint defends against typos."""
        from sqlalchemy.exc import IntegrityError
        now = datetime.now(UTC)
        with pytest.raises(IntegrityError), db_engine.begin() as conn:
            ExchangeStateRepository(conn).record_eod(
                "BSE", run_at=now, outcome="bogus",
                data_date=None, run_id=1,
            )

    def test_eod_does_not_touch_initial_load_fields(self, db_engine):
        """EoD writes must NOT overwrite the lifecycle markers."""
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            ExchangeStateRepository(conn).record_eod(
                "NSE", run_at=now, outcome="success",
                data_date=date(2026, 5, 21), run_id=100,
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("NSE")
        # Seed values preserved:
        assert row.initial_load_started_at is not None
        assert row.initial_load_completed_at is not None


# ---------- record_intraday / listings_sync / tier_recompute ---------- #


class TestNonEodWrites:

    def test_record_intraday(self, db_engine):
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            ExchangeStateRepository(conn).record_intraday(
                "BSE", run_at=now,
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.last_intraday_run_at == now
        # Other fields stay NULL
        assert row.last_eod_run_at is None

    def test_record_listings_sync(self, db_engine):
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            ExchangeStateRepository(conn).record_listings_sync(
                "BSE", run_at=now, run_id=7,
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.last_listings_sync_at == now
        assert row.last_listings_run_id == 7

    def test_record_tier_recompute(self, db_engine):
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            ExchangeStateRepository(conn).record_tier_recompute(
                "BSE", run_at=now,
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.last_tier_recompute_at == now


# ---------- initial_load lifecycle ---------- #


class TestInitialLoadLifecycle:
    """Lifecycle markers are write-once: subsequent backfills don't
    overwrite them. This is the key contract for the initial-load
    fields."""

    def test_started_at_set_on_first_call(self, db_engine):
        ts = datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            ExchangeStateRepository(conn).record_backfill_started(
                "BSE", started_at=ts,
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.initial_load_started_at == ts

    def test_started_at_not_overwritten_on_second_call(self, db_engine):
        ts1 = datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 5, 22, 0, 0, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            repo = ExchangeStateRepository(conn)
            repo.record_backfill_started("BSE", started_at=ts1)
            repo.record_backfill_started("BSE", started_at=ts2)
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.initial_load_started_at == ts1  # not ts2

    def test_completed_at_set_only_on_success(self, db_engine):
        ts = datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            repo = ExchangeStateRepository(conn)
            repo.record_backfill_started("BSE", started_at=ts)
            repo.record_backfill_completed(
                "BSE", completed_at=ts, outcome="failed",
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.initial_load_completed_at is None

    def test_completed_at_set_on_success(self, db_engine):
        start = datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC)
        end = datetime(2026, 5, 21, 1, 0, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            repo = ExchangeStateRepository(conn)
            repo.record_backfill_started("BSE", started_at=start)
            repo.record_backfill_completed(
                "BSE", completed_at=end, outcome="success",
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.initial_load_completed_at == end

    def test_completed_at_not_overwritten_by_later_backfill(self, db_engine):
        """Once initial_load_completed_at is set, a later backfill
        (e.g. operator re-running for safety) does NOT update it."""
        first = datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC)
        second = datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            repo = ExchangeStateRepository(conn)
            repo.record_backfill_started("BSE", started_at=first)
            repo.record_backfill_completed(
                "BSE", completed_at=first, outcome="success",
            )
            # Operator re-runs backfill later
            repo.record_backfill_started("BSE", started_at=second)
            repo.record_backfill_completed(
                "BSE", completed_at=second, outcome="success",
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.initial_load_started_at == first    # not second
        assert row.initial_load_completed_at == first  # not second

    def test_started_then_failed_then_succeeded(self, db_engine):
        """First backfill fails, operator retries, second succeeds.
        initial_load_completed_at should reflect the SECOND attempt's
        success — that's the genuine completion. initial_load_started_at
        should stay on the first attempt's timestamp (load began then)."""
        start1 = datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC)
        end1 = datetime(2026, 5, 21, 1, 0, 0, tzinfo=UTC)
        start2 = datetime(2026, 5, 22, 0, 0, 0, tzinfo=UTC)
        end2 = datetime(2026, 5, 22, 1, 0, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            repo = ExchangeStateRepository(conn)
            repo.record_backfill_started("BSE", started_at=start1)
            repo.record_backfill_completed(
                "BSE", completed_at=end1, outcome="failed",
            )
            repo.record_backfill_started("BSE", started_at=start2)
            repo.record_backfill_completed(
                "BSE", completed_at=end2, outcome="success",
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.initial_load_started_at == start1
        assert row.initial_load_completed_at == end2


# ---------- check constraints ---------- #


class TestCheckConstraints:

    def test_completed_before_started_rejected(self, db_engine):
        """exchange_state_initial_load_order_chk: completed must be >= started."""
        from sqlalchemy.exc import IntegrityError
        start = datetime(2026, 5, 22, 0, 0, 0, tzinfo=UTC)
        end_before = datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC)
        with pytest.raises(IntegrityError), db_engine.begin() as conn:
            # Raw insert to bypass the repo (which doesn't expose
            # this direct combo). The CHECK constraint catches it.
            conn.execute(
                text("""
                        INSERT INTO exchange_state (
                            exchange,
                            initial_load_started_at,
                            initial_load_completed_at
                        ) VALUES ('LSE', :start, :end)
                    """),
                {"start": start, "end": end_before},
            )

    def test_null_completed_with_non_null_started_allowed(self, db_engine):
        """A load that started but never completed is a real state."""
        start = datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            ExchangeStateRepository(conn).record_backfill_started(
                "LSE", started_at=start,
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("LSE")
        assert row.initial_load_started_at == start
        assert row.initial_load_completed_at is None


# ---------- field isolation ---------- #


class TestFieldIsolation:
    """A write to one field must NOT clobber adjacent fields."""

    def test_intraday_after_eod_preserves_eod_fields(self, db_engine):
        eod_ts = datetime(2026, 5, 21, 1, 0, 0, tzinfo=UTC)
        intraday_ts = datetime(2026, 5, 21, 10, 30, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            repo = ExchangeStateRepository(conn)
            repo.record_eod(
                "BSE", run_at=eod_ts, outcome="success",
                data_date=date(2026, 5, 20), run_id=10,
            )
            repo.record_intraday("BSE", run_at=intraday_ts)
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        # EoD fields preserved:
        assert row.last_eod_run_at == eod_ts
        assert row.last_eod_outcome == "success"
        assert row.last_eod_data_date == date(2026, 5, 20)
        assert row.last_eod_run_id == 10
        # Intraday field set:
        assert row.last_intraday_run_at == intraday_ts

    def test_listings_sync_after_eod_preserves_eod_fields(self, db_engine):
        eod_ts = datetime(2026, 5, 21, 1, 0, 0, tzinfo=UTC)
        listings_ts = datetime(2026, 5, 21, 18, 0, 0, tzinfo=UTC)
        with db_engine.begin() as conn:
            repo = ExchangeStateRepository(conn)
            repo.record_eod(
                "BSE", run_at=eod_ts, outcome="success",
                data_date=date(2026, 5, 20), run_id=10,
            )
            repo.record_listings_sync(
                "BSE", run_at=listings_ts, run_id=11,
            )
        with db_engine.connect() as conn:
            row = ExchangeStateRepository(conn).get("BSE")
        assert row.last_eod_run_at == eod_ts
        assert row.last_eod_run_id == 10
        assert row.last_listings_sync_at == listings_ts
        assert row.last_listings_run_id == 11
