"""Integration tests for `trip_ema.persistence.backfill_runs_repo`.

Verifies:
  - Schema (migration 0028 landed): table + indexes present
  - start/finish lifecycle
  - is_running / active_runs reflect the marker state
  - Unique partial index rejects a second concurrent start
  - release_locks clears orphans
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, UTC

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from trip_ema.persistence import transaction
from trip_ema.persistence.backfill_runs_repo import (
    BackfillRunRecord,
    BackfillRunsRepository,
)


# ============================================================
# Helpers
# ============================================================

def _start(
    conn, *,
    exchange: str = "NSE",
    date_from: date = date(2024, 1, 1),
    date_to: date = date(2024, 12, 31),
    refetch_bars: bool = True,
    ticker_scope: str = "ALL",
) -> int:
    return BackfillRunsRepository(conn).start(
        pid=os.getpid(),
        exchange=exchange,
        date_from=date_from,
        date_to=date_to,
        refetch_bars=refetch_bars,
        ticker_scope=ticker_scope,
    )


# ============================================================
# Schema invariants
# ============================================================

class TestSchema:

    def test_table_present(self, db_engine):
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_name = 'backfill_runs'
            """)).fetchone()
        assert row is not None

    def test_unique_partial_index_present(self, db_engine):
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT indexname
                  FROM pg_indexes
                 WHERE tablename = 'backfill_runs'
                   AND indexname = 'uq_backfill_runs_one_active'
            """)).fetchone()
        assert row is not None

    def test_active_index_present(self, db_engine):
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT indexname
                  FROM pg_indexes
                 WHERE tablename = 'backfill_runs'
                   AND indexname = 'idx_backfill_runs_active'
            """)).fetchone()
        assert row is not None


# ============================================================
# Lifecycle
# ============================================================

class TestStartFinish:

    def test_start_returns_run_id(self, db_engine):
        with transaction(db_engine) as conn:
            rid = _start(conn)
        assert isinstance(rid, int)
        assert rid > 0

    def test_started_at_default_is_now(self, db_engine):
        before = datetime.now(UTC)
        with transaction(db_engine) as conn:
            rid = _start(conn)
        after = datetime.now(UTC)

        with db_engine.connect() as conn:
            record = BackfillRunsRepository(conn).get(rid)
        assert record is not None
        assert before - timedelta(seconds=2) <= record.started_at
        assert record.started_at <= after + timedelta(seconds=2)
        assert record.finished_at is None

    def test_finish_sets_finished_at(self, db_engine):
        with transaction(db_engine) as conn:
            rid = _start(conn)
        before = datetime.now(UTC)
        with transaction(db_engine) as conn:
            ok = BackfillRunsRepository(conn).finish(rid)
        after = datetime.now(UTC)
        assert ok is True

        with db_engine.connect() as conn:
            record = BackfillRunsRepository(conn).get(rid)
        assert record is not None
        assert record.finished_at is not None
        assert (
            before - timedelta(seconds=2)
            <= record.finished_at
            <= after + timedelta(seconds=2)
        )

    def test_finish_with_explicit_when(self, db_engine):
        when = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
        with transaction(db_engine) as conn:
            rid = _start(conn)
        with transaction(db_engine) as conn:
            BackfillRunsRepository(conn).finish(rid, when=when)

        with db_engine.connect() as conn:
            record = BackfillRunsRepository(conn).get(rid)
        assert record.finished_at == when

    def test_finish_already_finished_returns_false(self, db_engine):
        with transaction(db_engine) as conn:
            rid = _start(conn)
        with transaction(db_engine) as conn:
            assert BackfillRunsRepository(conn).finish(rid) is True
        with transaction(db_engine) as conn:
            assert BackfillRunsRepository(conn).finish(rid) is False

    def test_finish_unknown_run_returns_false(self, db_engine):
        with transaction(db_engine) as conn:
            assert (
                BackfillRunsRepository(conn).finish(99999) is False
            )

    def test_start_persists_all_fields(self, db_engine):
        with transaction(db_engine) as conn:
            rid = BackfillRunsRepository(conn).start(
                pid=4242,
                exchange="BSE",
                date_from=date(2023, 6, 15),
                date_to=date(2024, 6, 14),
                refetch_bars=False,
                ticker_scope="RELIANCE,TCS",
            )
        with db_engine.connect() as conn:
            rec = BackfillRunsRepository(conn).get(rid)
        assert rec is not None
        assert rec.pid == 4242
        assert rec.exchange == "BSE"
        assert rec.date_from == date(2023, 6, 15)
        assert rec.date_to == date(2024, 6, 14)
        assert rec.refetch_bars is False
        assert rec.ticker_scope == "RELIANCE,TCS"


# ============================================================
# is_running / active_runs
# ============================================================

class TestRunningState:

    def test_is_running_false_when_no_rows(self, db_engine):
        with db_engine.connect() as conn:
            assert (
                BackfillRunsRepository(conn).is_running() is False
            )

    def test_is_running_true_with_active_row(self, db_engine):
        with transaction(db_engine) as conn:
            _start(conn)
        with db_engine.connect() as conn:
            assert (
                BackfillRunsRepository(conn).is_running() is True
            )

    def test_is_running_false_after_finish(self, db_engine):
        with transaction(db_engine) as conn:
            rid = _start(conn)
            BackfillRunsRepository(conn).finish(rid)
        with db_engine.connect() as conn:
            assert (
                BackfillRunsRepository(conn).is_running() is False
            )

    def test_active_runs_returns_unfinished_only(self, db_engine):
        # Finish one, start another
        with transaction(db_engine) as conn:
            rid1 = _start(conn)
            BackfillRunsRepository(conn).finish(rid1)
        with transaction(db_engine) as conn:
            rid2 = _start(conn, date_from=date(2025, 1, 1))

        with db_engine.connect() as conn:
            active = BackfillRunsRepository(conn).active_runs()
        assert len(active) == 1
        assert active[0].run_id == rid2

    def test_active_runs_empty_when_no_marker(self, db_engine):
        with db_engine.connect() as conn:
            assert (
                BackfillRunsRepository(conn).active_runs() == []
            )


# ============================================================
# Unique partial index
# ============================================================

class TestUniqueActiveConstraint:

    def test_second_start_while_active_raises(self, db_engine):
        """The unique partial index `uq_backfill_runs_one_active`
        prevents two unfinished rows. Fail-closed semantics for
        accidental double-start."""
        with transaction(db_engine) as conn:
            _start(conn)

        with pytest.raises(IntegrityError):
            with transaction(db_engine) as conn:
                _start(conn, date_from=date(2025, 1, 1))

    def test_second_start_after_finish_succeeds(self, db_engine):
        with transaction(db_engine) as conn:
            rid1 = _start(conn)
            BackfillRunsRepository(conn).finish(rid1)

        with transaction(db_engine) as conn:
            rid2 = _start(conn, date_from=date(2025, 1, 1))

        assert rid2 != rid1


# ============================================================
# release_locks
# ============================================================

class TestReleaseLocks:

    def test_release_locks_no_active_rows(self, db_engine):
        with transaction(db_engine) as conn:
            n = BackfillRunsRepository(conn).release_locks()
        assert n == 0

    def test_release_locks_clears_orphan(self, db_engine):
        with transaction(db_engine) as conn:
            _start(conn)
        with db_engine.connect() as conn:
            assert (
                BackfillRunsRepository(conn).is_running() is True
            )

        with transaction(db_engine) as conn:
            n = BackfillRunsRepository(conn).release_locks()
        assert n == 1

        with db_engine.connect() as conn:
            assert (
                BackfillRunsRepository(conn).is_running() is False
            )

    def test_release_locks_does_not_touch_finished(self, db_engine):
        # One finished, one orphan
        with transaction(db_engine) as conn:
            rid1 = _start(conn)
            BackfillRunsRepository(conn).finish(rid1)
        with transaction(db_engine) as conn:
            rid2 = _start(conn, date_from=date(2025, 1, 1))

        # Capture the original finished_at of rid1
        with db_engine.connect() as conn:
            rec1_before = BackfillRunsRepository(conn).get(rid1)

        with transaction(db_engine) as conn:
            n = BackfillRunsRepository(conn).release_locks()
        assert n == 1   # only the orphan was cleared

        with db_engine.connect() as conn:
            rec1_after = BackfillRunsRepository(conn).get(rid1)
            rec2_after = BackfillRunsRepository(conn).get(rid2)
        # rid1's finished_at unchanged
        assert rec1_after.finished_at == rec1_before.finished_at
        # rid2 now has finished_at set
        assert rec2_after.finished_at is not None
