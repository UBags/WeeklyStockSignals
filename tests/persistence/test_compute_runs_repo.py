"""Tests for ComputeRunsRepository (Step 13a).

The repo backs the ETL Status GUI's Activity Log. Each row is one
orchestrator tick (intraday or eod) OR one backfill invocation.
Tests pin the lifecycle:
  - start() inserts a row with finished_at = NULL
  - finish() sets finished_at, counts, error info
  - recent() returns newest-first, respects limit
  - in_flight() finds NULL-finished_at rows for crash recovery
  - kind CHECK constraint enforces 'intraday'|'eod'|'backfill'
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from trip_ema.persistence import transaction
from trip_ema.persistence.compute_runs_repo import (
    ComputeRunRow,
    ComputeRunsRepository,
)


class TestStartFinish:

    def test_start_returns_id_and_leaves_finished_null(
        self, db_engine,
    ):
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
            row = ComputeRunsRepository(conn).get(run_id)
        assert row is not None
        assert row.kind == "intraday"
        assert row.exchange == "NSE"
        assert row.started_at is not None
        assert row.finished_at is None
        assert row.is_in_flight is True
        assert row.duration_seconds is None

    def test_finish_sets_finished_at(self, db_engine):
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="eod", exchange="NSE",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).finish(
                run_id,
                tickers_attempted=2667,
                tickers_fetched=2496,
            )
        with db_engine.connect() as conn:
            row = ComputeRunsRepository(conn).get(run_id)
        assert row is not None
        assert row.finished_at is not None
        assert row.tickers_attempted == 2667
        assert row.tickers_fetched == 2496
        assert row.failed_stage is None
        assert row.error_message is None
        assert row.is_in_flight is False
        assert row.is_success is True
        assert row.duration_seconds is not None
        assert row.duration_seconds >= 0

    def test_finish_with_error(self, db_engine):
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).finish(
                run_id,
                failed_stage="stage_5_industry_state",
                error_message="UnsupportedExchangeError: BSE",
            )
        with db_engine.connect() as conn:
            row = ComputeRunsRepository(conn).get(run_id)
        assert row is not None
        assert row.failed_stage == "stage_5_industry_state"
        assert row.error_message.startswith("Unsupported")
        assert row.is_success is False
        assert row.is_in_flight is False

    def test_double_finish_is_safe(self, db_engine):
        """Idempotent finish — calling twice is safe (UPDATE
        succeeds; finished_at gets the latest NOW())."""
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).finish(run_id)
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).finish(
                run_id, error_message="ignored",
            )
        # No exception — both finishes succeeded.


class TestKindValidation:

    def test_invalid_kind_rejected_python_side(self, db_engine):
        """Python-side validation catches typos before they hit
        the DB CHECK constraint."""
        with pytest.raises(ValueError, match="kind must be one of"):
            with transaction(db_engine) as conn:
                ComputeRunsRepository(conn).start(
                    kind="not_a_kind", exchange="NSE",
                )

    def test_all_three_kinds_accepted(self, db_engine):
        for kind in ("intraday", "eod", "backfill"):
            with transaction(db_engine) as conn:
                ComputeRunsRepository(conn).start(
                    kind=kind, exchange="NSE",
                )
        with db_engine.connect() as conn:
            rows = ComputeRunsRepository(conn).recent(n=10)
        kinds = {r.kind for r in rows}
        assert {"intraday", "eod", "backfill"}.issubset(kinds)


class TestRecent:

    def test_recent_returns_newest_first(self, db_engine):
        with transaction(db_engine) as conn:
            ids = [
                ComputeRunsRepository(conn).start(
                    kind="intraday", exchange="NSE",
                )
                for _ in range(5)
            ]
        with db_engine.connect() as conn:
            rows = ComputeRunsRepository(conn).recent(n=5)
        # Newest first; the last id we inserted should come first
        assert [r.compute_run_id for r in rows] == list(reversed(ids))

    def test_recent_respects_limit(self, db_engine):
        with transaction(db_engine) as conn:
            for _ in range(10):
                ComputeRunsRepository(conn).start(
                    kind="intraday", exchange="NSE",
                )
        with db_engine.connect() as conn:
            rows = ComputeRunsRepository(conn).recent(n=3)
        assert len(rows) == 3

    def test_recent_zero_limit_returns_empty(self, db_engine):
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
        with db_engine.connect() as conn:
            rows = ComputeRunsRepository(conn).recent(n=0)
        assert rows == []

    def test_recent_filters_by_exchange(self, db_engine):
        with transaction(db_engine) as conn:
            nse_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
            bse_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="BSE",
            )
        with db_engine.connect() as conn:
            nse_rows = ComputeRunsRepository(conn).recent(
                n=10, exchange="NSE",
            )
        ids = {r.compute_run_id for r in nse_rows}
        assert nse_id in ids
        assert bse_id not in ids


class TestInFlight:

    def test_in_flight_finds_unfinished(self, db_engine):
        with transaction(db_engine) as conn:
            # 2 in-flight (not finished)
            id1 = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
            id2 = ComputeRunsRepository(conn).start(
                kind="eod", exchange="NSE",
            )
            # 1 finished
            id3 = ComputeRunsRepository(conn).start(
                kind="backfill", exchange="NSE",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).finish(id3)

        with db_engine.connect() as conn:
            rows = ComputeRunsRepository(conn).in_flight(
                exchange="NSE",
            )
        ids = {r.compute_run_id for r in rows}
        assert id1 in ids
        assert id2 in ids
        assert id3 not in ids

    def test_in_flight_returns_empty_after_all_finished(
        self, db_engine,
    ):
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).finish(run_id)

        with db_engine.connect() as conn:
            rows = ComputeRunsRepository(conn).in_flight(
                exchange="NSE",
            )
        # Only the just-inserted row, which is now finished
        assert all(r.compute_run_id != run_id for r in rows)


class TestRowAccessors:

    def test_duration_seconds_when_finished(self, db_engine):
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
        # Tiny delay so duration > 0
        import time
        time.sleep(0.01)
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).finish(run_id)
        with db_engine.connect() as conn:
            row = ComputeRunsRepository(conn).get(run_id)
        assert row.duration_seconds is not None
        assert row.duration_seconds >= 0.01

    def test_get_returns_none_for_missing(self, db_engine):
        with db_engine.connect() as conn:
            row = ComputeRunsRepository(conn).get(
                compute_run_id=999_999_999,
            )
        assert row is None


class TestCurrentStage:
    """PHASE-3F: compute_runs.current_stage — live per-stage marker
    read by the ETL Status GUI to surface auto-run progress."""

    def test_start_leaves_current_stage_null(self, db_engine):
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
            row = ComputeRunsRepository(conn).get(run_id)
        assert row is not None
        assert row.current_stage is None

    def test_set_current_stage_round_trips(self, db_engine):
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).set_current_stage(run_id, "iiv")
        with db_engine.connect() as conn:
            row = ComputeRunsRepository(conn).get(run_id)
        assert row is not None
        assert row.current_stage == "iiv"

    def test_set_current_stage_visible_in_in_flight(self, db_engine):
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).set_current_stage(
                run_id, "fetch_bars",
            )
        with db_engine.connect() as conn:
            rows = ComputeRunsRepository(conn).in_flight(exchange="NSE")
        match = [r for r in rows if r.compute_run_id == run_id]
        assert match, "expected the in-flight row to be present"
        assert match[0].current_stage == "fetch_bars"

    def test_set_current_stage_none_clears(self, db_engine):
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="intraday", exchange="NSE",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).set_current_stage(
                run_id, "notify",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).set_current_stage(run_id, None)
        with db_engine.connect() as conn:
            row = ComputeRunsRepository(conn).get(run_id)
        assert row is not None
        assert row.current_stage is None

    def test_finish_nulls_current_stage(self, db_engine):
        with transaction(db_engine) as conn:
            run_id = ComputeRunsRepository(conn).start(
                kind="eod", exchange="NSE",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).set_current_stage(
                run_id, "industry_state",
            )
        with transaction(db_engine) as conn:
            ComputeRunsRepository(conn).finish(
                run_id, tickers_attempted=10, tickers_fetched=10,
            )
        with db_engine.connect() as conn:
            row = ComputeRunsRepository(conn).get(run_id)
        assert row is not None
        assert row.finished_at is not None
        assert row.current_stage is None
