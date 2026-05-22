"""Tests for QuarterlyFundamentalsScheduler.

We use the `now_fn` injection point to test the calendar logic
without waiting for actual quarter-ends, and we use a stubbed runner
to confirm the scheduler invokes it for the right (phase, exchange)
combinations.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from trip_ema.fundamentals.scheduler import (
    QUARTERLY_WINDOWS,
    QuarterlyFundamentalsScheduler,
    RECENT_RUN_DAYS,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import FundamentalsRunRepository


# ---------- fixtures ---------- #


@pytest.fixture
def fake_config():
    """A minimal config exposing two configured exchanges (NSE, BSE)."""
    cfg = MagicMock()
    nse, bse = MagicMock(), MagicMock()
    nse.exchange = "NSE"
    bse.exchange = "BSE"
    cfg.market_hours = [nse, bse]
    return cfg


def _at(year: int, month: int, day: int) -> datetime:
    """Helper: build a UTC datetime at the given date."""
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)


# ---------- in_refresh_window ---------- #


class TestRefreshWindow:

    def test_in_window_at_q4_filing_start(self, db_engine, fake_config):
        """30 April is one of the quarter-end+1month dates."""
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
        )
        assert sched._in_refresh_window() is True

    def test_in_window_30_days_after_q4_start(self, db_engine, fake_config):
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 5, 30),
        )
        assert sched._in_refresh_window() is True

    def test_not_in_window_far_before(self, db_engine, fake_config):
        """April 20: after the Jan-31 window's tail (April 1), before
        the Apr-30 window opens. One of the few real dead zones."""
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 20),
        )
        assert sched._in_refresh_window() is False

    def test_not_in_window_far_after(self, db_engine, fake_config):
        """Mid-September is after July+60 days, before October."""
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 9, 30),
        )
        # July 31 + 60 days ≈ September 29 — so Sep 30 should be just out.
        assert sched._in_refresh_window() is False

    def test_in_window_at_each_of_the_four_quarter_dates(
        self, db_engine, fake_config,
    ):
        for month, day in QUARTERLY_WINDOWS:
            sched = QuarterlyFundamentalsScheduler(
                db_engine, fake_config,
                now_fn=lambda m=month, d=day: _at(2026, m, d),
            )
            assert sched._in_refresh_window() is True, (
                f"{month:02d}-{day:02d} should be in refresh window"
            )


# ---------- skip_due_to_recent_run ---------- #


class TestSkipDueToRecentRun:

    def test_no_prior_run_means_dont_skip(self, db_engine, fake_config):
        sched = QuarterlyFundamentalsScheduler(db_engine, fake_config)
        assert sched._skip_due_to_recent_run("industry", "NSE") is False

    def test_recent_success_run_means_skip(self, db_engine, fake_config):
        """A SUCCESS run from 30 days ago → still within RECENT_RUN_DAYS."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="industry", exchange="NSE")
            r.close(rid, status="SUCCESS", succeeded=10, failed=0, skipped=0)
        sched = QuarterlyFundamentalsScheduler(db_engine, fake_config)
        assert sched._skip_due_to_recent_run("industry", "NSE") is True

    def test_old_success_run_means_dont_skip(self, db_engine, fake_config):
        """A SUCCESS run from > RECENT_RUN_DAYS ago → re-run is due."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="industry", exchange="NSE")
            r.close(rid, status="SUCCESS", succeeded=10, failed=0, skipped=0)
            # Backdate completed_at past the threshold.
            from sqlalchemy import text
            conn.execute(text(
                "UPDATE fundamentals_run SET completed_at = NOW() - INTERVAL '120 days' "
                "WHERE run_id = :rid"
            ), {"rid": rid})
        sched = QuarterlyFundamentalsScheduler(db_engine, fake_config)
        assert sched._skip_due_to_recent_run("industry", "NSE") is False

    def test_failed_run_does_not_count_as_recent(self, db_engine, fake_config):
        """Only SUCCESS rows count; a FAILED run from yesterday should
        NOT prevent a fresh attempt."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="industry", exchange="NSE")
            r.close(rid, status="FAILED", succeeded=0, failed=5, skipped=0)
        sched = QuarterlyFundamentalsScheduler(db_engine, fake_config)
        assert sched._skip_due_to_recent_run("industry", "NSE") is False


# ---------- _tick ---------- #


class TestTick:

    def test_tick_outside_window_does_nothing(self, db_engine, fake_config):
        """If we're not in a refresh window, _tick is a no-op even if
        a run is overdue."""
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 20),   # March 1 — outside all windows
        )
        with patch(
            "trip_ema.fundamentals.scheduler.run_industry_refresh",
        ) as mock_runner:
            sched._tick()
        assert mock_runner.call_count == 0

    def test_tick_in_window_fires_for_each_exchange(
        self, db_engine, fake_config,
    ):
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
        )
        with patch(
            "trip_ema.fundamentals.scheduler.run_industry_refresh",
        ) as mock_runner:
            sched._tick()
        # Two exchanges configured (NSE, BSE) — one fire each.
        assert mock_runner.call_count == 2
        called_exchanges = sorted(
            call.args[1] for call in mock_runner.call_args_list
        )
        assert called_exchanges == ["BSE", "NSE"]

    def test_tick_skips_exchange_with_recent_success(
        self, db_engine, fake_config,
    ):
        """NSE has a recent success → don't re-fire it. BSE has none → fire."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="industry", exchange="NSE")
            r.close(rid, status="SUCCESS", succeeded=10, failed=0, skipped=0)
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
        )
        with patch(
            "trip_ema.fundamentals.scheduler.run_industry_refresh",
        ) as mock_runner:
            sched._tick()
        assert mock_runner.call_count == 1
        # Only BSE was called.
        assert mock_runner.call_args_list[0].args[1] == "BSE"

    def test_tick_handles_runner_exception(self, db_engine, fake_config):
        """If the runner raises for one exchange, we still try the next."""
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
        )
        call_log = []
        def raising_runner(engine, exchange, **kw):
            call_log.append(exchange)
            if exchange == "BSE":
                raise RuntimeError("simulated failure")
            return 1
        with patch(
            "trip_ema.fundamentals.scheduler.run_industry_refresh",
            side_effect=raising_runner,
        ):
            sched._tick()
        # Both exchanges should have been attempted.
        assert set(call_log) == {"NSE", "BSE"}


# ---------- start/stop lifecycle ---------- #


class TestLifecycle:

    def test_start_spawns_thread(self, db_engine, fake_config):
        # Very long wakeup so the loop blocks on the stop event — the
        # test just verifies the thread starts. We also point now_fn
        # at a date outside any window so the first tick does no work.
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            wakeup_seconds=60.0,
            now_fn=lambda: _at(2026, 4, 20),
        )
        sched.start()
        try:
            assert sched.is_alive
        finally:
            sched.stop(timeout=2.0)
        assert not sched.is_alive

    def test_start_is_idempotent(self, db_engine, fake_config):
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            wakeup_seconds=60.0,
            now_fn=lambda: _at(2026, 4, 20),
        )
        sched.start()
        thread1 = sched._thread
        sched.start()  # second start — should be a no-op
        assert sched._thread is thread1
        sched.stop(timeout=2.0)

    def test_stop_is_idempotent(self, db_engine, fake_config):
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            wakeup_seconds=60.0,
            now_fn=lambda: _at(2026, 4, 20),
        )
        sched.start()
        sched.stop(timeout=2.0)
        sched.stop(timeout=2.0)  # second stop — no error

    def test_stop_wakes_sleeping_thread(self, db_engine, fake_config):
        """A 10-minute wakeup must not block stop() from returning
        quickly — the loop should be sleeping on the stop event,
        not on time.sleep()."""
        import time
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            wakeup_seconds=600.0,
            now_fn=lambda: _at(2026, 4, 20),
        )
        sched.start()
        # Let the loop enter its sleep.
        time.sleep(0.05)
        t0 = time.monotonic()
        sched.stop(timeout=2.0)
        elapsed = time.monotonic() - t0
        # Should return well under 1 second despite the 10-minute wakeup.
        assert elapsed < 1.0, f"stop() took {elapsed:.2f}s; expected <1s"


# ---------- Phase 2: shares scheduling ---------- #


class TestSchedulerFiresShares:
    """The scheduler now fires BOTH industry and shares refreshes
    at each quarterly window. These tests exercise the new branch."""

    def test_tick_fires_both_industry_and_shares_per_exchange(
        self, db_engine, fake_config,
    ):
        """In a refresh window, with no prior runs and two configured
        exchanges, we expect 2 industry fires + 2 shares fires = 4
        runner calls in total."""
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
        )
        with patch(
            "trip_ema.fundamentals.scheduler.run_industry_refresh",
        ) as ind_mock, patch(
            "trip_ema.fundamentals.runners.run_shares_refresh",
        ) as shr_mock:
            sched._tick()
        assert ind_mock.call_count == 2  # NSE + BSE
        assert shr_mock.call_count == 2  # NSE + BSE

    def test_shares_phase_respects_skip_if_recent(self, db_engine, fake_config):
        """A recent SUCCESS shares run for NSE means the scheduler
        should fire shares only for BSE (not NSE). Industry should
        still fire for both since the recent run was for the shares
        phase."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="shares", exchange="NSE")
            r.close(rid, status="SUCCESS",
                    succeeded=5, failed=0, skipped=0)
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
        )
        with patch(
            "trip_ema.fundamentals.scheduler.run_industry_refresh",
        ) as ind_mock, patch(
            "trip_ema.fundamentals.runners.run_shares_refresh",
        ) as shr_mock:
            sched._tick()
        # Industry fires for both — no industry recent runs.
        assert ind_mock.call_count == 2
        # Shares fires only for BSE — NSE was just done.
        assert shr_mock.call_count == 1
        assert shr_mock.call_args_list[0].args[1] == "BSE"

    def test_unknown_phase_in_scheduled_phases_is_skipped_safely(
        self, db_engine, fake_config, monkeypatch,
    ):
        """Defensive: if SCHEDULED_PHASES were ever to grow a phase
        for which there's no runner, _fire() should warn and skip
        rather than crash."""
        # Temporarily extend SCHEDULED_PHASES to include something
        # we don't have a runner for.
        monkeypatch.setattr(
            "trip_ema.fundamentals.scheduler.SCHEDULED_PHASES",
            ("industry", "shares", "nonexistent_phase"),
        )
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
        )
        with patch(
            "trip_ema.fundamentals.scheduler.run_industry_refresh",
        ), patch(
            "trip_ema.fundamentals.runners.run_shares_refresh",
        ):
            # Should not raise.
            sched._tick()
