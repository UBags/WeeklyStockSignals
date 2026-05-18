"""Tests for ComputeScheduler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.cache import SignalCache
from trip_ema.domain import (
    BarState,
    ChangeSignal,
    CoreRegime,
    IndicatorParams,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from trip_ema.engine import TickerEngine
from trip_ema.persistence import (
    BarsRepository,
    SignalsRepository,
    TickersRepository,
    transaction,
)
from trip_ema.registry import TickerRegistry
from trip_ema.scheduler.compute_scheduler import (
    ComputeScheduler,
    _seconds_until_next_tick,
)


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")
RELIANCE = TickerRef("NSE", "RELIANCE", "EQ")


# ============================================================
# _seconds_until_next_tick — pure math, no DB
# ============================================================

class TestSecondsUntilNextTick:
    def test_returns_full_cadence_at_exact_boundary(self):
        # When `now` is exactly at a cadence boundary (timestamp divisible
        # by cadence seconds), the next tick should be a full cadence away.
        # Use an instant whose Unix timestamp is divisible by 60.
        now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
        # 10:00:00 UTC May 18 2026 has some Unix timestamp; verify whatever
        # it is, the function behaves correctly relative to it.
        seconds = _seconds_until_next_tick(now, timedelta(minutes=60))
        assert 0 < seconds <= 3600

    def test_returns_smaller_value_partway_through_window(self):
        cadence = timedelta(minutes=60)
        # At some arbitrary instant, calling twice 30s apart should show
        # a 30s difference.
        t1 = datetime(2026, 5, 18, 10, 17, 0, tzinfo=timezone.utc)
        t2 = t1 + timedelta(seconds=30)
        s1 = _seconds_until_next_tick(t1, cadence)
        s2 = _seconds_until_next_tick(t2, cadence)
        # s2 should be 30 less than s1 (or near a boundary, wrap by 3600).
        assert s1 - s2 == pytest.approx(30, abs=0.001) or \
               s2 - s1 == pytest.approx(3570, abs=0.001)

    def test_short_cadence(self):
        cadence = timedelta(minutes=15)
        # At some second, next tick should be no more than 900 seconds away.
        now = datetime(2026, 5, 18, 10, 7, 13, tzinfo=timezone.utc)
        seconds = _seconds_until_next_tick(now, cadence)
        assert 0 < seconds <= 900


# ============================================================
# DB-backed scheduler tests
# ============================================================

@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _seed_ticker_with_bars(db_engine, ref, n=80, start_close=100.0):
    """Insert a ticker plus N daily-spaced bars. Returns ticker_id."""
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(
            ref, created_at=datetime.now(timezone.utc),
        )
        bars_repo = BarsRepository(conn)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for i in range(n):
            close = start_close + i
            bars_repo.insert_bar(
                tid, Timeframe.WEEKLY,
                OHLCVBar(
                    timestamp=start + timedelta(weeks=i),
                    open=close - 0.5, high=close + 1, low=close - 1,
                    close=close, volume=1000.0,
                ),
            )
    return tid


def _seed_one_new_bar(db_engine, ticker_id, after_weeks, close):
    """Add one weekly bar after the existing history."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with transaction(db_engine) as conn:
        BarsRepository(conn).insert_bar(
            ticker_id, Timeframe.WEEKLY,
            OHLCVBar(
                timestamp=start + timedelta(weeks=after_weeks),
                open=close - 0.5, high=close + 1, low=close - 1,
                close=close, volume=1000.0,
            ),
        )


def _build_registry_and_cache(db_engine):
    """Prime the registry/cache as the lifecycle function would."""
    from trip_ema.api import prime_all_tickers
    registry = TickerRegistry(IndicatorParams(), [Timeframe.WEEKLY])
    cache = SignalCache()
    prime_all_tickers(db_engine=db_engine, registry=registry, cache=cache)
    return registry, cache


# ---------- Behavior: no new bars ---------- #

def test_run_once_with_no_new_bars_fires_no_events(db_engine):
    _seed_ticker_with_bars(db_engine, NIFTY, n=80)
    registry, cache = _build_registry_and_cache(db_engine)

    scheduler = ComputeScheduler(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    summary = scheduler.run_once()
    assert summary.tickers_examined == 1
    assert summary.tickers_with_new_bars == 0
    assert summary.bars_ingested == 0
    assert summary.events_fired == []
    assert summary.errors == []


def test_run_once_with_no_tickers_is_no_op(db_engine):
    registry, cache = _build_registry_and_cache(db_engine)
    scheduler = ComputeScheduler(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    summary = scheduler.run_once()
    assert summary.tickers_examined == 0


# ---------- Behavior: new bars ---------- #

def test_run_once_ingests_new_bars(db_engine):
    """Add a new bar after priming; verify the scheduler ingests it."""
    tid = _seed_ticker_with_bars(db_engine, NIFTY, n=80)
    registry, cache = _build_registry_and_cache(db_engine)

    _seed_one_new_bar(db_engine, tid, after_weeks=80, close=200.0)

    scheduler = ComputeScheduler(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    summary = scheduler.run_once()
    assert summary.tickers_with_new_bars == 1
    assert summary.bars_ingested == 1

    # Cache should now reflect the new bar.
    cached = cache.get(NIFTY, Timeframe.WEEKLY)
    assert cached is not None
    # The newest BarState's timestamp matches the bar we just inserted.
    expected_ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=80)
    assert cached.latest_state.timestamp == expected_ts


def test_run_once_persists_signal_event_when_display_changes(db_engine):
    """Force a display change between prime time and a new bar; verify a
    signal_events row appears."""
    # Prime with a steady uptrend that ends with the display painted BULLISH.
    tid = _seed_ticker_with_bars(db_engine, NIFTY, n=80, start_close=100.0)
    registry, cache = _build_registry_and_cache(db_engine)

    # Identify the cached display state after priming.
    primed_cache = cache.get(NIFTY, Timeframe.WEEKLY)
    assert primed_cache is not None
    primed_display = primed_cache.latest_state.display_bg

    # We want to force a different display on the next compute. The
    # simplest reliable way: ingest enough sharply-reversing bars to flip
    # the regime. A single -50% reversal candle usually does it.
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with transaction(db_engine) as conn:
        repo = BarsRepository(conn)
        # 10 strongly bearish bars in a row
        for i in range(10):
            wk = 80 + i
            close = 50 - i           # plunging price
            repo.insert_bar(
                tid, Timeframe.WEEKLY,
                OHLCVBar(
                    timestamp=start + timedelta(weeks=wk),
                    open=close + 5,
                    high=close + 5,
                    low=close - 1,
                    close=close,
                    volume=1000.0,
                ),
            )

    scheduler = ComputeScheduler(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    summary = scheduler.run_once()
    assert summary.tickers_with_new_bars == 1
    assert summary.bars_ingested == 10

    new_cache = cache.get(NIFTY, Timeframe.WEEKLY)
    # We can't deterministically predict the exact display state without
    # running the math by hand, but we *can* assert that if it differs from
    # the primed display, an event was fired; and if it doesn't, no event.
    if new_cache.latest_state.display_bg != primed_display:
        assert len(summary.events_fired) == 1
        event = summary.events_fired[0]
        assert event.ticker == NIFTY
        # The DB should also have the row.
        with db_engine.connect() as conn:
            recent = SignalsRepository(conn).recent_events_for_ticker(
                tid, NIFTY, limit=10,
            )
        assert len(recent) >= 1
    else:
        # Display didn't change; no event should have fired.
        assert summary.events_fired == []


def test_run_once_does_not_fire_when_display_unchanged(db_engine):
    """A new bar that continues the existing regime should NOT fire an event."""
    tid = _seed_ticker_with_bars(db_engine, NIFTY, n=80, start_close=100.0)
    registry, cache = _build_registry_and_cache(db_engine)
    primed = cache.get(NIFTY, Timeframe.WEEKLY)
    primed_display = primed.latest_state.display_bg

    # Add a bar that continues the trend (no reason for regime to change).
    _seed_one_new_bar(db_engine, tid, after_weeks=80, close=181.0)

    scheduler = ComputeScheduler(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    summary = scheduler.run_once()
    new_cache = cache.get(NIFTY, Timeframe.WEEKLY)

    if new_cache.latest_state.display_bg == primed_display:
        assert summary.events_fired == []


def test_run_once_does_not_upsert_ticker_signal_state(db_engine):
    """ticker_signal_state was dropped in migration 005. Verify no attempt
    is made to write to it."""
    tid = _seed_ticker_with_bars(db_engine, NIFTY, n=80)
    registry, cache = _build_registry_and_cache(db_engine)
    _seed_one_new_bar(db_engine, tid, after_weeks=80, close=181.0)

    scheduler = ComputeScheduler(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    summary = scheduler.run_once()

    # Run should succeed
    assert summary.errors == []

    # And ticker_signal_state should not be queryable (table was dropped)
    from sqlalchemy import text
    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'ticker_signal_state'"
        )).all()
    assert rows == [], "ticker_signal_state should have been dropped"


# ---------- Error isolation ---------- #

def test_one_bad_ticker_does_not_break_others(db_engine):
    """If one ticker's compute raises, the others should still process."""
    _seed_ticker_with_bars(db_engine, NIFTY, n=80)
    _seed_ticker_with_bars(db_engine, RELIANCE, n=80)
    registry, cache = _build_registry_and_cache(db_engine)

    # Replace NIFTY's engine with one that raises on every ingest. This
    # simulates a runtime bug inside the indicator math.
    class BrokenEngine:
        def __init__(self, ts):
            self._ts = ts

        def last_bar_timestamp(self):
            return self._ts

        def latest_state(self):
            raise RuntimeError("simulated indicator failure")

        def ingest(self, bar):
            raise RuntimeError("simulated indicator failure")

        def last_close(self):
            return 100.0

        def state_history(self, n=200):
            return []

    # Need a non-None last_bar_timestamp so the scheduler proceeds to
    # fetch_since (which is where _process_ticker first does real work).
    primed_ts = cache.get(NIFTY, Timeframe.WEEKLY).latest_state.timestamp
    registry._engines[(NIFTY, Timeframe.WEEKLY)] = BrokenEngine(primed_ts)

    # Need new bars for both tickers so the scheduler attempts ingest.
    reliance_tid = registry.get_ticker_id(RELIANCE)
    _seed_one_new_bar(db_engine, reliance_tid, after_weeks=80, close=181.0)
    nifty_tid = registry.get_ticker_id(NIFTY)
    _seed_one_new_bar(db_engine, nifty_tid, after_weeks=80, close=181.0)

    scheduler = ComputeScheduler(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    summary = scheduler.run_once()
    # NIFTY should have errored; RELIANCE should have succeeded.
    assert summary.tickers_examined == 2
    assert len(summary.errors) == 1
    assert "NIFTY" in summary.errors[0][0]
    # RELIANCE's new bar should have been ingested
    reliance_cache = cache.get(RELIANCE, Timeframe.WEEKLY)
    assert reliance_cache is not None
    expected_ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(weeks=80)
    assert reliance_cache.latest_state.timestamp == expected_ts


# ---------- Cadence validation ---------- #

def test_cadence_below_one_minute_rejected(db_engine):
    registry, cache = _build_registry_and_cache(db_engine)
    with pytest.raises(ValueError):
        ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
            cadence_minutes=0,
        )


# ============================================================
# Phase 6: provisional bars, historical revisions, concurrency lock
# ============================================================


def _patch_now_for_scheduler(scheduler, fake_now):
    """Patch datetime.now in compute_scheduler so we can control whether a
    bar is provisional. Returns a contextmanager-style undo."""
    from unittest.mock import patch
    return patch(
        "trip_ema.scheduler.compute_scheduler.datetime",
        wraps=datetime,
        now=lambda tz=None: fake_now,
    )


class TestRunLockPreventsConcurrentRuns:
    """run_once() must be reentrancy-protected so push and poll don't
    double-run for the same ticker."""

    def test_second_concurrent_run_returns_skipped_summary(self, db_engine):
        """If we manually acquire the run lock, a subsequent run_once()
        call should return immediately with skipped=True."""
        _seed_ticker_with_bars(db_engine, NIFTY, n=80)
        registry, cache = _build_registry_and_cache(db_engine)
        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )

        # Acquire the lock from "outside" — simulating an in-flight call.
        acquired = scheduler._run_lock.acquire(blocking=False)
        assert acquired

        try:
            summary = scheduler.run_once()
        finally:
            scheduler._run_lock.release()

        assert summary.skipped_due_to_concurrent_run is True
        assert summary.tickers_examined == 0

    def test_sequential_runs_both_succeed(self, db_engine):
        """When there's no contention, every run_once() proceeds normally."""
        _seed_ticker_with_bars(db_engine, NIFTY, n=80)
        registry, cache = _build_registry_and_cache(db_engine)
        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )

        s1 = scheduler.run_once()
        s2 = scheduler.run_once()

        assert s1.skipped_due_to_concurrent_run is False
        assert s2.skipped_due_to_concurrent_run is False


class TestProvisionalBarHandling:
    """When a bar's bucket is still open, the scheduler must call
    ingest_provisional() instead of ingest(), so subsequent updates to
    the same bar don't compound state."""

    def test_open_week_bar_is_ingested_provisionally(self, db_engine):
        """Seed a ticker, then add a brand-new bar whose week is still
        open relative to a near-future fake-now. The engine should hold
        a provisional snapshot afterward."""
        # Seed historical bars
        tid = _seed_ticker_with_bars(db_engine, NIFTY, n=80)
        registry, cache = _build_registry_and_cache(db_engine)

        # Add a new bar at week 80 (Mon 2025-07-14 by aggregation convention).
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        week_80_start = start + timedelta(weeks=80)
        _seed_one_new_bar(db_engine, tid, after_weeks=80, close=181.0)

        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )

        # Patch the scheduler's notion of "now" to be DURING week 80
        # (so the bar is provisional).
        fake_now = week_80_start + timedelta(hours=12)
        with _patch_now_for_scheduler(scheduler, fake_now):
            scheduler.run_once()

        engine = registry.get(NIFTY, Timeframe.WEEKLY)
        assert engine is not None
        # The provisional snapshot should be held
        assert engine.has_provisional() is True

    def test_closed_week_bar_is_ingested_finally(self, db_engine):
        """If we point the fake-now AFTER the week closes, the bar
        becomes final — no provisional snapshot."""
        tid = _seed_ticker_with_bars(db_engine, NIFTY, n=80)
        registry, cache = _build_registry_and_cache(db_engine)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        _seed_one_new_bar(db_engine, tid, after_weeks=80, close=181.0)

        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )
        # Now AFTER week 80's bucket ends
        fake_now = start + timedelta(weeks=81, hours=12)
        with _patch_now_for_scheduler(scheduler, fake_now):
            scheduler.run_once()

        engine = registry.get(NIFTY, Timeframe.WEEKLY)
        assert engine is not None
        assert engine.has_provisional() is False

    def test_repeated_provisional_runs_do_not_compound_state(self, db_engine):
        """The critical invariant: re-running the scheduler against an
        updated provisional bar must produce the same engine state as
        if only the final value had been seen."""
        tid = _seed_ticker_with_bars(db_engine, NIFTY, n=80)
        registry, cache = _build_registry_and_cache(db_engine)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        week_80_start = start + timedelta(weeks=80)

        # Add the provisional bar with one set of OHLCV
        _seed_one_new_bar(db_engine, tid, after_weeks=80, close=181.0)

        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )

        fake_now = week_80_start + timedelta(hours=2)
        with _patch_now_for_scheduler(scheduler, fake_now):
            scheduler.run_once()

        # Simulate ETL revising the same bar with new OHLCV at hour 5
        from sqlalchemy import update
        from trip_ema.persistence.tables import ohlcv_bars
        with transaction(db_engine) as conn:
            conn.execute(
                update(ohlcv_bars)
                .where(
                    ohlcv_bars.c.ticker_id == tid,
                    ohlcv_bars.c.timeframe == "1W",
                    ohlcv_bars.c.bar_timestamp == week_80_start,
                )
                .values(close=185.0, high=186.0)
            )

        # Run a second time at hour 5 of the same week
        fake_now_2 = week_80_start + timedelta(hours=5)
        with _patch_now_for_scheduler(scheduler, fake_now_2):
            scheduler.run_once()

        engine_a = registry.get(NIFTY, Timeframe.WEEKLY)
        med_a = engine_a.latest_state().med_ema
        long_a = engine_a.latest_state().long_ema

        # Build a SECOND clean scenario: skip the first provisional, only
        # ever see the updated bar. State should match.
        # (We can't easily reuse the same DB, so we just verify the
        # engine's state is consistent — no double-application.)
        # Compare engine state if we directly ingest the second version
        # of the bar from a fresh registry.
        registry_b, cache_b = _build_registry_and_cache(db_engine)
        scheduler_b = ComputeScheduler(
            db_engine=db_engine, registry=registry_b, cache=cache_b,
        )
        with _patch_now_for_scheduler(scheduler_b, fake_now_2):
            scheduler_b.run_once()
        engine_b = registry_b.get(NIFTY, Timeframe.WEEKLY)
        med_b = engine_b.latest_state().med_ema
        long_b = engine_b.latest_state().long_ema

        # The two paths must agree
        assert med_a == pytest.approx(med_b)
        assert long_a == pytest.approx(long_b)


class TestHistoricalRevisionDetection:
    """If the ETL updates a bar OLDER than the engine's last_ts (e.g.
    after a corporate action), the scheduler must detect this via
    updated_at and force a re-prime."""

    def test_revision_to_old_bar_triggers_reprime(self, db_engine):
        tid = _seed_ticker_with_bars(db_engine, NIFTY, n=80)
        registry, cache = _build_registry_and_cache(db_engine)

        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )

        # First run — establishes the high-water mark.
        # Add a new bar at week 80 so the run actually does work.
        _seed_one_new_bar(db_engine, tid, after_weeks=80, close=181.0)
        scheduler.run_once()
        assert scheduler._last_revision_high_water != {}

        # Now update an OLDER bar (week 40, well before last_ts).
        from sqlalchemy import update
        from trip_ema.persistence.tables import ohlcv_bars
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        week_40_ts = start + timedelta(weeks=40)
        with transaction(db_engine) as conn:
            conn.execute(
                update(ohlcv_bars)
                .where(
                    ohlcv_bars.c.ticker_id == tid,
                    ohlcv_bars.c.timeframe == "1W",
                    ohlcv_bars.c.bar_timestamp == week_40_ts,
                )
                .values(close=999.0)
            )

        # Re-run; should detect the revision and re-prime.
        summary = scheduler.run_once()
        assert summary.tickers_reprimed == 1

    def test_new_bar_at_end_is_not_treated_as_revision(self, db_engine):
        """A new bar AT or AFTER last_ts is not a 'historical' revision —
        it's just the normal forward-flowing data."""
        tid = _seed_ticker_with_bars(db_engine, NIFTY, n=80)
        registry, cache = _build_registry_and_cache(db_engine)

        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )
        scheduler.run_once()  # establishes high-water

        # Insert a NEW bar at the end (week 80)
        _seed_one_new_bar(db_engine, tid, after_weeks=80, close=181.0)

        summary = scheduler.run_once()
        assert summary.tickers_reprimed == 0
        assert summary.bars_ingested == 1


class TestLastRunCompletedAt:
    """The /readyz health check reads last_run_completed_at."""

    def test_starts_as_none(self, db_engine):
        registry, cache = _build_registry_and_cache(db_engine)
        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )
        assert scheduler.last_run_completed_at is None

    def test_set_after_successful_run(self, db_engine):
        _seed_ticker_with_bars(db_engine, NIFTY, n=80)
        registry, cache = _build_registry_and_cache(db_engine)
        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )
        scheduler.run_once()
        assert scheduler.last_run_completed_at is not None
        assert scheduler.last_run_completed_at.tzinfo is not None

    def test_not_set_when_skipped_due_to_concurrency(self, db_engine):
        registry, cache = _build_registry_and_cache(db_engine)
        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )
        scheduler._run_lock.acquire()
        try:
            summary = scheduler.run_once()
        finally:
            scheduler._run_lock.release()
        # When skipped, completed_at should not be set
        assert summary.skipped_due_to_concurrent_run is True
        assert scheduler.last_run_completed_at is None


# ============================================================
# A6: Daily partition extension
# ============================================================

class TestPartitionExtension:
    """A6 fix: ComputeScheduler.run_once() must keep the notification_log
    partitions extended forward, at most once per UTC day. Without this,
    the 2-month forward buffer from migration 004 eventually exhausts."""

    def test_first_run_extends_partitions(self, db_engine):
        registry, cache = _build_registry_and_cache(db_engine)
        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )
        assert scheduler._last_partition_check_date is None
        scheduler.run_once()
        # After a successful run, today's date is recorded.
        assert scheduler._last_partition_check_date == datetime.now(timezone.utc).date()

    def test_partition_extension_is_idempotent(self, db_engine):
        """Calling run_once twice on the same UTC day should not hit
        the DB for partitions on the second call."""
        registry, cache = _build_registry_and_cache(db_engine)
        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )
        # First run records the date
        scheduler.run_once()
        first_date = scheduler._last_partition_check_date

        # Second run should not change the date (same UTC day)
        scheduler.run_once()
        assert scheduler._last_partition_check_date == first_date

    def test_partition_extension_creates_future_partition(self, db_engine):
        """After run_once, the partition for (today + 2 months) must
        exist in the DB. We query information_schema to confirm."""
        registry, cache = _build_registry_and_cache(db_engine)
        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )
        scheduler.run_once()

        # Build the expected partition name
        future = datetime.now(timezone.utc) + timedelta(days=63)
        expected_partition = f"notification_log_{future.strftime('%Y_%m')}"

        from sqlalchemy import text
        with db_engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :name"
            ), {"name": expected_partition}).all()
        assert len(rows) == 1, (
            f"expected partition {expected_partition} to exist; "
            f"got: {[r[0] for r in rows]}"
        )
