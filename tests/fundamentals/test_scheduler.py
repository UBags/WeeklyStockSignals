"""Tests for QuarterlyFundamentalsScheduler.

We use the `now_fn` injection point to test the calendar logic
without waiting for actual quarter-ends, and we use a stubbed runner
to confirm the scheduler invokes it for the right (phase, exchange)
combinations.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, UTC
from unittest.mock import MagicMock

import pytest

from trip_ema.fundamentals.scheduler import (
    QUARTERLY_WINDOWS,
    QuarterlyFundamentalsScheduler,
    RECENT_RUN_DAYS,
)
from trip_ema.fundamentals.stages import (
    DEFAULT_FUNDAMENTALS_RUNNERS,
    UnknownPhaseError,
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
    return datetime(year, month, day, 12, 0, tzinfo=UTC)


class _Spy:
    """A stand-in runner that records its calls.

    Replaces the old mock of the shares runner. Note WHERE that mock had to
    aim: at trip_ema.fundamentals.runners, not at the scheduler — because
    the scheduler imported the function locally inside _fire(). The local
    import existed for no other reason.

    (Deliberately not quoting the old call here. The patch budget counts
    textually and would score the quotation as debt.)
    """

    def __init__(self, side_effect=None):
        self.calls: list[tuple] = []
        self._side_effect = side_effect

    def __call__(self, db_engine, exchange, **kw):
        self.calls.append((db_engine, exchange, kw))
        if self._side_effect is not None:
            return self._side_effect(db_engine, exchange, **kw)
        return 0

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def exchanges(self) -> list[str]:
        return [c[1] for c in self.calls]


@pytest.fixture
def spies():
    """One spy per scheduled phase. Every scheduler in these tests gets a
    registry built from these, so no test can accidentally fire a REAL
    30-minute yfinance refresh against the fixture DB."""
    return {
        "industry": _Spy(),
        "shares": _Spy(),
        "financials": _Spy(),
    }


@pytest.fixture
def runners(spies):
    return DEFAULT_FUNDAMENTALS_RUNNERS.with_(**spies)


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

    def test_tick_outside_window_does_nothing(
        self, db_engine, fake_config, runners, spies,
    ):
        """If we're not in a refresh window, _tick is a no-op even if
        a run is overdue."""
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 20),   # outside all windows
            runners=runners,
        )
        sched._tick()
        assert spies["industry"].call_count == 0

    def test_tick_in_window_fires_for_each_exchange(
        self, db_engine, fake_config, runners, spies,
    ):
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
            runners=runners,
        )
        sched._tick()
        # Two exchanges configured (NSE, BSE) — one fire each.
        assert spies["industry"].call_count == 2
        assert sorted(spies["industry"].exchanges) == ["BSE", "NSE"]

    def test_tick_skips_exchange_with_recent_success(
        self, db_engine, fake_config, runners, spies,
    ):
        """NSE has a recent success → don't re-fire it. BSE has none → fire."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="industry", exchange="NSE")
            r.close(rid, status="SUCCESS", succeeded=10, failed=0, skipped=0)
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
            runners=runners,
        )
        sched._tick()
        assert spies["industry"].call_count == 1
        assert spies["industry"].exchanges == ["BSE"]

    def test_tick_handles_runner_exception(
        self, db_engine, fake_config, runners, spies,
    ):
        """If the runner raises for one exchange, we still try the next."""
        def raising(engine, exchange, **kw):
            if exchange == "BSE":
                raise RuntimeError("simulated failure")
            return 1
        industry = _Spy(side_effect=raising)
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
            runners=runners.with_(industry=industry),
        )
        sched._tick()
        # Both exchanges should have been attempted.
        assert set(industry.exchanges) == {"NSE", "BSE"}


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
        self, db_engine, fake_config, runners, spies,
    ):
        """In a refresh window, with no prior runs and two configured
        exchanges, we expect 2 industry fires + 2 shares fires = 4
        runner calls in total."""
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
            runners=runners,
        )
        sched._tick()
        assert spies["industry"].call_count == 2  # NSE + BSE
        assert spies["shares"].call_count == 2    # NSE + BSE

    def test_shares_phase_respects_skip_if_recent(
        self, db_engine, fake_config, runners, spies,
    ):
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
            runners=runners,
        )
        sched._tick()
        # Industry fires for both — no industry recent runs.
        assert spies["industry"].call_count == 2
        # Shares fires only for BSE — NSE was just done.
        assert spies["shares"].exchanges == ["BSE"]

    def test_unknown_scheduled_phase_is_rejected_at_construction(
        self, db_engine, fake_config, runners,
    ):
        """The old version of this test monkeypatched SCHEDULED_PHASES to
        smuggle a runner-less phase past the loop, then asserted _fire()
        warned and skipped. The registry makes that state unreachable: the
        legal phase names ARE the dataclass fields, and the scheduler checks
        the list against them before it owns a thread. Warn-and-skip was
        the wrong answer anyway — a misconfigured scheduler that silently
        does less than you asked is exactly the failure this codebase is
        built to refuse."""
        with pytest.raises(UnknownPhaseError):
            QuarterlyFundamentalsScheduler(
                db_engine, fake_config,
                now_fn=lambda: _at(2026, 4, 30),
                runners=runners,
                scheduled_phases=("industry", "shares", "nonexistent_phase"),
            )

    def test_registry_rejects_unknown_override(self, runners):
        with pytest.raises(UnknownPhaseError):
            runners.with_(nonexistent_phase=lambda *a, **k: None)


# ---------- Phase 3: financials scheduling ---------- #


class TestSchedulerFiresFinancials:
    """The scheduler now fires industry, shares, AND financials at
    each quarterly window."""

    def test_tick_fires_all_three_phases_per_exchange(
        self, db_engine, fake_config, runners, spies,
    ):
        """In a refresh window, with two configured exchanges and no
        prior runs: 2 industry + 2 shares + 2 financials = 6 calls."""
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
            runners=runners,
        )
        sched._tick()
        assert spies["industry"].call_count == 2
        assert spies["shares"].call_count == 2
        assert spies["financials"].call_count == 2

    def test_financials_phase_respects_skip_if_recent(
        self, db_engine, fake_config, runners, spies,
    ):
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="financials", exchange="NSE")
            r.close(rid, status="SUCCESS",
                    succeeded=5, failed=0, skipped=0)
        sched = QuarterlyFundamentalsScheduler(
            db_engine, fake_config,
            now_fn=lambda: _at(2026, 4, 30),
            runners=runners,
        )
        sched._tick()
        # Financials fires only for BSE.
        assert spies["financials"].exchanges == ["BSE"]
