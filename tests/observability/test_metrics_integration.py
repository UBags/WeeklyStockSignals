"""Integration test for scheduler metrics — Step 9 replacement.

The OLD test (`test_metrics_integration.py`, deleted) exercised the
TickerEngine ingestion path and asserted on per-bar and per-engine
counters that the new ComputeScheduler doesn't drive any more
(bars_ingested_total, signal_events_total, tickers_reprimed_total).

This replacement covers the metrics the **new** `run_once()` path
actually emits:

  - `scheduler_runs_total{outcome=completed}` increments once per
    successful pass.
  - `scheduler_runs_total{outcome=skipped_concurrent}` increments
    when a second `run_once()` races a first.
  - `scheduler_run_duration_seconds` records a wall-clock sample
    per completed pass.
  - `scheduler_tickers_examined_total` increments once per active
    ticker seen during the pass.

Metrics that the old path drove but the new path doesn't (yet):
bars_ingested_total, signal_events_total, errors_total. These are
intentionally untested here — the new scheduler delegates to Step 6
which has its own per-ticker test coverage that doesn't go through
the Prometheus REGISTRY. A future enhancement would have Step 6
report these via the scheduler's summary so the scheduler can
increment them itself; that's out of Step 9 scope.

Requires a real Postgres (uses the `db_engine` fixture from the
persistence conftest).
"""

from __future__ import annotations

import threading
import time

import pytest

from trip_ema.cache import SignalCache
from trip_ema.observability import metrics as M
from trip_ema.scheduler import ComputeScheduler


# ---------------------------------------------------------------------------
# Metric snapshot helpers — read the float values directly off the
# private accessors. The prometheus_client public API surface for
# reading values is awkward (you have to scrape and parse text). These
# accessors are stable across the 0.x line.
# ---------------------------------------------------------------------------


def _counter_value(counter, **labels) -> float:
    """Read a Counter's current value. If the counter has labelnames,
    pass them as kwargs."""
    if labels:
        sample = counter.labels(**labels)
    else:
        sample = counter
    return sample._value.get()


def _histogram_count(histogram) -> int:
    """Read a Histogram's total observation count. Equivalent to
    the `_count` time series Prometheus exposes.

    `_buckets` in prometheus_client are PER-bucket counts (not the
    cumulative form Prometheus exposes), so the total count is the
    sum across all buckets — small observations land in low buckets
    and never reach +Inf.
    """
    return int(sum(b.get() for b in histogram._buckets))


@pytest.fixture
def _metric_snapshot():
    """Capture starting values of the metrics this test asserts on, so
    each test can reason in deltas. Necessary because the metric
    REGISTRY is module-level and persists across tests."""
    return {
        "runs_completed": _counter_value(
            M.scheduler_runs_total, outcome="completed",
        ),
        "runs_skipped": _counter_value(
            M.scheduler_runs_total, outcome="skipped_concurrent",
        ),
        "duration_count": _histogram_count(M.scheduler_run_duration_seconds),
        "tickers_examined": _counter_value(M.scheduler_tickers_examined_total),
    }


# ---------------------------------------------------------------------------
# Schema setup — the scheduler queries `tickers WHERE is_active`. Build
# the minimal table; the rest of the schema is irrelevant for the
# scheduler's metric emission (Step 6 may return skip_reason for any
# ticker without bars, but it won't crash).
# ---------------------------------------------------------------------------


@pytest.fixture
def _tickers_table(db_engine):
    """Minimal `tickers` table — just enough columns for
    `TickersRepository.find_active` to return rows."""
    with db_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS tickers CASCADE")
        conn.exec_driver_sql("""
            CREATE TABLE tickers (
                ticker_id SERIAL PRIMARY KEY,
                exchange  VARCHAR(8)  NOT NULL,
                symbol    VARCHAR(64) NOT NULL,
                instrument VARCHAR(8) NOT NULL,
                is_active BOOLEAN     NOT NULL DEFAULT TRUE,
                full_name VARCHAR,
                tz        VARCHAR(32) NOT NULL DEFAULT 'UTC',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    yield db_engine
    with db_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS tickers CASCADE")


# ===========================================================================
# Tests
# ===========================================================================


class TestRunCompletionMetrics:

    def test_run_once_increments_runs_completed(
        self, _tickers_table, _metric_snapshot,
    ):
        """One successful pass → one increment on the completed counter
        and one new observation on the duration histogram."""
        cache = SignalCache()
        sched = ComputeScheduler(
            db_engine=_tickers_table, cache=cache, cadence_minutes=60,
        )

        sched.run_once()

        runs_after = _counter_value(
            M.scheduler_runs_total, outcome="completed",
        )
        dur_after = _histogram_count(M.scheduler_run_duration_seconds)

        assert runs_after - _metric_snapshot["runs_completed"] == 1, (
            "scheduler_runs_total{outcome=completed} should have "
            "incremented by 1"
        )
        assert dur_after - _metric_snapshot["duration_count"] == 1, (
            "scheduler_run_duration_seconds should have one new "
            "observation"
        )

    def test_run_once_empty_db_examines_zero_tickers(
        self, _tickers_table, _metric_snapshot,
    ):
        """No active tickers → tickers_examined_total unchanged."""
        cache = SignalCache()
        sched = ComputeScheduler(
            db_engine=_tickers_table, cache=cache, cadence_minutes=60,
        )

        sched.run_once()

        examined_after = _counter_value(M.scheduler_tickers_examined_total)
        assert examined_after == _metric_snapshot["tickers_examined"], (
            "tickers_examined_total should not have moved with no "
            "active tickers in the DB"
        )


class TestTickerExaminationMetrics:

    def test_one_active_ticker_increments_examined_counter(
        self, _tickers_table, _metric_snapshot,
    ):
        """Add one active ticker — the next pass should examine it,
        bumping tickers_examined_total by 1. The ticker has no bars
        so Step 6 returns skip_reason='no_bars', but the examination
        counter still fires (it counts attempts, not successes)."""
        with _tickers_table.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO tickers "
                "(exchange, symbol, instrument, is_active, tz) "
                "VALUES ('NSE', 'RELIANCE', 'EQ', TRUE, 'Asia/Kolkata')"
            )

        cache = SignalCache()
        sched = ComputeScheduler(
            db_engine=_tickers_table, cache=cache, cadence_minutes=60,
        )
        sched.run_once()

        examined_after = _counter_value(M.scheduler_tickers_examined_total)
        delta = examined_after - _metric_snapshot["tickers_examined"]
        assert delta == 1, (
            f"expected tickers_examined_total to increment by 1, got "
            f"delta={delta}"
        )

    def test_multiple_active_tickers_increment_examined_counter(
        self, _tickers_table, _metric_snapshot,
    ):
        """Three active tickers → examined counter +3."""
        with _tickers_table.begin() as conn:
            for sym in ("RELIANCE", "TCS", "INFY"):
                conn.exec_driver_sql(
                    "INSERT INTO tickers "
                    "(exchange, symbol, instrument, is_active, tz) "
                    f"VALUES ('NSE', '{sym}', 'EQ', TRUE, 'Asia/Kolkata')"
                )

        cache = SignalCache()
        sched = ComputeScheduler(
            db_engine=_tickers_table, cache=cache, cadence_minutes=60,
        )
        sched.run_once()

        examined_after = _counter_value(M.scheduler_tickers_examined_total)
        delta = examined_after - _metric_snapshot["tickers_examined"]
        assert delta == 3, (
            f"expected tickers_examined_total to increment by 3, got "
            f"delta={delta}"
        )

    def test_inactive_tickers_are_not_examined(
        self, _tickers_table, _metric_snapshot,
    ):
        """is_active=FALSE rows are filtered out by the scheduler's
        ticker query."""
        with _tickers_table.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO tickers "
                "(exchange, symbol, instrument, is_active, tz) VALUES "
                "('NSE', 'ACTIVE',   'EQ', TRUE,  'Asia/Kolkata'), "
                "('NSE', 'INACTIVE', 'EQ', FALSE, 'Asia/Kolkata')"
            )

        cache = SignalCache()
        sched = ComputeScheduler(
            db_engine=_tickers_table, cache=cache, cadence_minutes=60,
        )
        sched.run_once()

        examined_after = _counter_value(M.scheduler_tickers_examined_total)
        delta = examined_after - _metric_snapshot["tickers_examined"]
        assert delta == 1, (
            f"expected only the active ticker to be examined, got "
            f"delta={delta}"
        )


class TestReentrancyMetrics:

    def test_concurrent_run_increments_skipped_counter(
        self, _tickers_table, _metric_snapshot,
    ):
        """If a second `run_once()` races a first, it returns immediately
        with `skipped_due_to_concurrent_run=True` and increments
        `scheduler_runs_total{outcome=skipped_concurrent}` — NOT
        `outcome=completed`."""
        cache = SignalCache()
        sched = ComputeScheduler(
            db_engine=_tickers_table, cache=cache, cadence_minutes=60,
        )

        # Acquire the run_lock manually to simulate an in-flight pass,
        # then run_once() from the main thread should hit the
        # `if not self._run_lock.acquire(blocking=False)` branch.
        acquired = sched._run_lock.acquire(blocking=True)
        assert acquired
        try:
            summary = sched.run_once()
        finally:
            sched._run_lock.release()

        assert summary.skipped_due_to_concurrent_run is True, (
            "second run_once() should have set the skip flag"
        )
        skipped_after = _counter_value(
            M.scheduler_runs_total, outcome="skipped_concurrent",
        )
        delta = skipped_after - _metric_snapshot["runs_skipped"]
        assert delta == 1, (
            f"expected runs_total{{outcome=skipped_concurrent}} to "
            f"increment by 1, got delta={delta}"
        )

        # And the completed counter should NOT have moved.
        completed_after = _counter_value(
            M.scheduler_runs_total, outcome="completed",
        )
        assert completed_after == _metric_snapshot["runs_completed"], (
            "skipped pass should not have incremented the completed "
            "counter"
        )


class TestDurationMetric:

    def test_duration_histogram_observes_realistic_time(
        self, _tickers_table, _metric_snapshot,
    ):
        """The histogram's sum should grow by a small but positive
        amount on each completed pass. Empty-DB pass should land in
        the lowest bucket (<10ms in practice, but allow up to 100ms
        for slow CI)."""
        cache = SignalCache()
        sched = ComputeScheduler(
            db_engine=_tickers_table, cache=cache, cadence_minutes=60,
        )

        sum_before = M.scheduler_run_duration_seconds._sum.get()
        sched.run_once()
        sum_after = M.scheduler_run_duration_seconds._sum.get()

        delta = sum_after - sum_before
        assert 0.0 < delta < 1.0, (
            f"expected a small positive duration sample, got "
            f"delta={delta:.4f}s"
        )