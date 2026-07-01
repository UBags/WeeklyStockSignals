"""DB-hit tests for UpdateRunInProgressRepository.

Covers the six-method surface: start, set_pid, delete, clear_all,
is_any_in_progress, get, list_all. Also the unique-index behavior
that prevents two rows per exchange.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from trip_ema.persistence import transaction
from trip_ema.persistence.update_run_in_progress_repo import (
    UpdateRunInProgressRecord,
    UpdateRunInProgressRepository,
)


def _clear(engine) -> None:
    with transaction(engine) as conn:
        conn.execute(text("DELETE FROM update_run_in_progress"))


# ============================================================
# start / get
# ============================================================

class TestStartAndGet:
    def test_start_inserts_row_returning_id(self, db_engine):
        _clear(db_engine)
        with transaction(db_engine) as conn:
            row_id = UpdateRunInProgressRepository(conn).start(
                exchange="NSE", kind="intraday",
            )
        assert isinstance(row_id, int)
        assert row_id > 0
        _clear(db_engine)

    def test_get_returns_full_record(self, db_engine):
        _clear(db_engine)
        with transaction(db_engine) as conn:
            UpdateRunInProgressRepository(conn).start(
                exchange="NSE", kind="eod", pid=12345,
            )
        with db_engine.connect() as conn:
            rec = UpdateRunInProgressRepository(conn).get("NSE")
        assert rec is not None
        assert isinstance(rec, UpdateRunInProgressRecord)
        assert rec.exchange == "NSE"
        assert rec.kind == "eod"
        assert rec.pid == 12345
        assert rec.started_at is not None
        _clear(db_engine)

    def test_get_missing_returns_none(self, db_engine):
        _clear(db_engine)
        with db_engine.connect() as conn:
            assert (
                UpdateRunInProgressRepository(conn).get("XXX") is None
            )

    def test_start_pid_defaults_to_null(self, db_engine):
        _clear(db_engine)
        with transaction(db_engine) as conn:
            UpdateRunInProgressRepository(conn).start(
                exchange="BSE", kind="intraday",
            )
        with db_engine.connect() as conn:
            rec = UpdateRunInProgressRepository(conn).get("BSE")
        assert rec is not None
        assert rec.pid is None
        _clear(db_engine)


# ============================================================
# set_pid
# ============================================================

class TestSetPid:
    def test_set_pid_updates_row(self, db_engine):
        _clear(db_engine)
        with transaction(db_engine) as conn:
            row_id = UpdateRunInProgressRepository(conn).start(
                exchange="NSE", kind="intraday",
            )
        with transaction(db_engine) as conn:
            UpdateRunInProgressRepository(conn).set_pid(row_id, 99999)
        with db_engine.connect() as conn:
            rec = UpdateRunInProgressRepository(conn).get("NSE")
        assert rec.pid == 99999
        _clear(db_engine)


# ============================================================
# Uniqueness enforcement
# ============================================================

class TestUniqueness:
    def test_second_start_for_same_exchange_raises_integrity_error(
        self, db_engine,
    ):
        _clear(db_engine)
        with transaction(db_engine) as conn:
            UpdateRunInProgressRepository(conn).start(
                exchange="NSE", kind="intraday",
            )
        with pytest.raises(IntegrityError):
            with transaction(db_engine) as conn:
                UpdateRunInProgressRepository(conn).start(
                    exchange="NSE", kind="eod",
                )
        _clear(db_engine)

    def test_different_exchanges_can_coexist(self, db_engine):
        # Q5 says one at a time globally today, but the schema
        # doesn't restrict that — future-proofing check.
        _clear(db_engine)
        with transaction(db_engine) as conn:
            UpdateRunInProgressRepository(conn).start(
                exchange="NSE", kind="intraday",
            )
            UpdateRunInProgressRepository(conn).start(
                exchange="NYSE", kind="eod",
            )
        with db_engine.connect() as conn:
            recs = UpdateRunInProgressRepository(conn).list_all()
        assert len(recs) == 2
        exchanges = {r.exchange for r in recs}
        assert exchanges == {"NSE", "NYSE"}
        _clear(db_engine)


# ============================================================
# CHECK constraint on kind
# ============================================================

class TestKindConstraint:
    def test_invalid_kind_rejected(self, db_engine):
        _clear(db_engine)
        with pytest.raises(IntegrityError):
            with transaction(db_engine) as conn:
                UpdateRunInProgressRepository(conn).start(
                    exchange="NSE", kind="backfill",   # not allowed
                )
        _clear(db_engine)


# ============================================================
# delete / clear_all
# ============================================================

class TestDelete:
    def test_delete_removes_row_returns_one(self, db_engine):
        _clear(db_engine)
        with transaction(db_engine) as conn:
            UpdateRunInProgressRepository(conn).start(
                exchange="NSE", kind="intraday",
            )
        with transaction(db_engine) as conn:
            n = UpdateRunInProgressRepository(conn).delete("NSE")
        assert n == 1
        with db_engine.connect() as conn:
            assert (
                UpdateRunInProgressRepository(conn).get("NSE") is None
            )
        _clear(db_engine)

    def test_delete_missing_returns_zero(self, db_engine):
        _clear(db_engine)
        with transaction(db_engine) as conn:
            n = UpdateRunInProgressRepository(conn).delete("XYZ")
        assert n == 0

    def test_clear_all_deletes_everything(self, db_engine):
        _clear(db_engine)
        with transaction(db_engine) as conn:
            r = UpdateRunInProgressRepository(conn)
            r.start(exchange="NSE", kind="intraday")
            r.start(exchange="BSE", kind="intraday")
            r.start(exchange="NYSE", kind="eod")

        with transaction(db_engine) as conn:
            n = UpdateRunInProgressRepository(conn).clear_all()
        assert n == 3

        with db_engine.connect() as conn:
            recs = UpdateRunInProgressRepository(conn).list_all()
        assert recs == []


# ============================================================
# is_any_in_progress
# ============================================================

class TestIsAnyInProgress:
    def test_false_when_table_empty(self, db_engine):
        _clear(db_engine)
        with db_engine.connect() as conn:
            assert (
                UpdateRunInProgressRepository(conn).is_any_in_progress()
                is False
            )

    def test_true_when_any_row_present(self, db_engine):
        _clear(db_engine)
        with transaction(db_engine) as conn:
            UpdateRunInProgressRepository(conn).start(
                exchange="NSE", kind="intraday",
            )
        with db_engine.connect() as conn:
            assert (
                UpdateRunInProgressRepository(conn).is_any_in_progress()
                is True
            )
        _clear(db_engine)


# ============================================================
# list_all
# ============================================================

class TestListAll:
    def test_returns_empty_when_no_rows(self, db_engine):
        _clear(db_engine)
        with db_engine.connect() as conn:
            assert UpdateRunInProgressRepository(conn).list_all() == []

    def test_ordered_by_started_at_asc(self, db_engine):
        import time as _time
        _clear(db_engine)
        with transaction(db_engine) as conn:
            r = UpdateRunInProgressRepository(conn)
            r.start(exchange="NSE", kind="intraday")
        _time.sleep(0.01)
        with transaction(db_engine) as conn:
            UpdateRunInProgressRepository(conn).start(
                exchange="BSE", kind="intraday",
            )
        with db_engine.connect() as conn:
            recs = UpdateRunInProgressRepository(conn).list_all()
        assert [r.exchange for r in recs] == ["NSE", "BSE"]
        _clear(db_engine)
