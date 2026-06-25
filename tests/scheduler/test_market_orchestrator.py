"""Tests for MarketOrchestrator.

Strategy:

* Pipeline behaviour (the 90% of the surface area) is tested via direct
  run_once() calls with mocked fetch/compute/distribute callables. No
  threads, no clock — fast and deterministic.

* Retry behaviour uses a tiny `fetch_retry_delay` (0.05s) so retries
  actually fire during a test run, but we still poll for completion
  with a generous wall-clock budget (5s) rather than fixed sleeps.

* Loop / start / stop lifecycle is tested separately, with the loop's
  `now_fn` and `sleep_fn` injected so we never wait real time.

* Tests do NOT exercise the real ComputeScheduler or YFinanceSource —
  those are tested elsewhere. The orchestrator's contract is purely
  about pipeline ordering, error isolation, retry policy, and timeout
  enforcement, all of which can be verified at the Callable interface.
"""

from __future__ import annotations

import time
from datetime import datetime, time as dt_time, timedelta, timezone, UTC
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from trip_ema.scheduler.market_calendar import (
    TradingCalendar,
    TradingCalendarConfig,
)
from trip_ema.scheduler.market_orchestrator import (
    MarketOrchestrator,
    OrchestratorRunSummary,
    OrchestratorSettings,
    OrchestratorStepResult,
)
from trip_ema.scheduler.tick_planner import MarketTickConfig


IST = ZoneInfo("Asia/Kolkata")
UTC = UTC


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _nse_market() -> MarketTickConfig:
    return MarketTickConfig(
        exchange="NSE",
        tz=IST,
        open=dt_time(9, 15),
        close=dt_time(15, 30),
        post_open_offset=timedelta(minutes=5),
        pre_close_offset=timedelta(minutes=15),
        intraday_cadence=timedelta(hours=1),
    )


def _mon_fri_calendar() -> TradingCalendar:
    """Calendar without algorithmic-holiday lookup; useful when we want
    full control over which dates are trading days."""
    return TradingCalendar(
        TradingCalendarConfig(
            exchange="NSE",
            weekdays=frozenset([1, 2, 3, 4, 5]),
        )
    )


def _settings(
    *,
    fetch_timeout_seconds: float = 5.0,
    fetch_retry_delay_seconds: float = 0.05,
    catch_up_on_startup: bool = False,
    catch_up_skip_if_recent_seconds: float = 30 * 60,
) -> OrchestratorSettings:
    return OrchestratorSettings(
        fetch_timeout=timedelta(seconds=fetch_timeout_seconds),
        fetch_retry_delay=timedelta(seconds=fetch_retry_delay_seconds),
        catch_up_on_startup=catch_up_on_startup,
        catch_up_skip_if_recent=timedelta(seconds=catch_up_skip_if_recent_seconds),
    )


# Module-level tracker for the autouse cleanup fixture. Each test gets
# a fresh empty list (the fixture clears+repopulates), and on teardown
# every orchestrator built during the test is stopped. Without this,
# daemon retry threads survive into subsequent tests and produce
# "cannot schedule new futures after shutdown" tracebacks from logger
# at interpreter shutdown — noisy and confusing, even though benign.
_built_orchestrators: list[MarketOrchestrator] = []


def _build(
    *,
    fetch_fn=None,
    compute_fn=None,
    distribute_fn=None,
    pulse=None,
    now: datetime | None = None,
    settings: OrchestratorSettings | None = None,
    calendar: TradingCalendar | None = None,
) -> MarketOrchestrator:
    """Convenience: fully-mocked orchestrator with sensible defaults.

    `now` fixes the clock for run_once() tests. If None, the real
    wall clock is used (fine for non-time-sensitive tests)."""
    fetch_fn = fetch_fn or MagicMock(return_value="run_id_42")
    compute_fn = compute_fn or MagicMock(return_value="compute_summary_obj")
    distribute_fn = distribute_fn or MagicMock(return_value="dist_summary_obj")
    if now is not None:
        clock = [now]
        now_fn = lambda: clock[0]
    else:
        now_fn = lambda: datetime.now(timezone.utc)
    orch = MarketOrchestrator(
        market=_nse_market(),
        calendar=calendar or _mon_fri_calendar(),
        settings=settings or _settings(),
        fetch_fn=fetch_fn,
        compute_run_once=compute_fn,
        distribute_run_once=distribute_fn,
        pulse=pulse,
        now_fn=now_fn,
        sleep_fn=lambda s: time.sleep(min(s, 0.01)),  # cap sleeps in tests
    )
    _built_orchestrators.append(orch)
    return orch


@pytest.fixture(autouse=True)
def _orchestrator_cleanup():
    """Stops every orchestrator built by this test at teardown."""
    _built_orchestrators.clear()
    yield
    for orch in _built_orchestrators:
        try:
            orch.stop(timeout=2.0)
        except Exception:
            pass
    _built_orchestrators.clear()


# Tuesday 2026-05-19, 10:30 IST = 05:00 UTC: mid-session, definitely trading day.
NOW_MID_SESSION = datetime(2026, 5, 19, 5, 0, 0, tzinfo=UTC)
# Saturday 2026-05-23, 10:30 IST = 05:00 UTC: NOT a trading day.
NOW_SATURDAY = datetime(2026, 5, 23, 5, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Pipeline ordering and success path
# ---------------------------------------------------------------------------


class TestPipelineSuccessPath:
    def test_all_three_steps_run_in_order_on_trading_day(self):
        calls = []
        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=lambda ex: calls.append(("fetch", ex)) or "rid",
            compute_fn=lambda: calls.append(("compute",)) or "csum",
            distribute_fn=lambda: calls.append(("distribute",)) or "dsum",
        )
        summary = orch.run_once()
        assert summary.all_steps_ok
        # Order: fetch first, then compute, then distribute.
        assert [c[0] for c in calls] == ["fetch", "compute", "distribute"]
        # Fetch received the exchange name.
        assert calls[0] == ("fetch", "NSE")

    def test_step_details_captured_in_summary(self):
        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=lambda ex: "fetch_run_id_42",
            compute_fn=lambda: "compute_summary",
            distribute_fn=lambda: "distribution_summary",
        )
        summary = orch.run_once()
        assert summary.fetch.detail == "fetch_run_id_42"
        assert summary.compute.detail == "compute_summary"
        assert summary.distribute.detail == "distribution_summary"

    def test_summary_has_correlation_id(self):
        orch = _build(now=NOW_MID_SESSION)
        summary = orch.run_once()
        assert summary.correlation_id != ""
        # UUID4 hex form is at least 32 chars; canonical with dashes is 36.
        assert len(summary.correlation_id) >= 32

    def test_summary_records_started_and_finished(self):
        orch = _build(now=NOW_MID_SESSION)
        summary = orch.run_once()
        assert summary.started_at is not None
        assert summary.finished_at is not None
        assert summary.finished_at >= summary.started_at

    def test_orchestrator_persists_last_run_completed_at(self):
        orch = _build(now=NOW_MID_SESSION)
        assert orch.last_run_completed_at is None
        summary = orch.run_once()
        assert orch.last_run_completed_at == summary.finished_at
        assert orch.last_run_summary is summary

    def test_step_durations_recorded(self):
        # Inject deliberate sleeps to ensure non-zero durations.
        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=lambda ex: (time.sleep(0.01), "rid")[1],
            compute_fn=lambda: (time.sleep(0.01), "csum")[1],
            distribute_fn=lambda: (time.sleep(0.01), "dsum")[1],
        )
        summary = orch.run_once()
        assert summary.fetch.duration_seconds > 0
        assert summary.compute.duration_seconds > 0
        assert summary.distribute.duration_seconds > 0


# ---------------------------------------------------------------------------
# Non-trading-day handling
# ---------------------------------------------------------------------------


class TestNonTradingDayBehavior:
    def test_saturday_skips_fetch_but_runs_compute_and_distribute(self):
        # On a non-trading day we still want compute+distribute to run:
        # any pending events from prior passes should still be delivered.
        calls = []
        orch = _build(
            now=NOW_SATURDAY,
            fetch_fn=lambda ex: calls.append("fetch") or "rid",
            compute_fn=lambda: calls.append("compute") or "csum",
            distribute_fn=lambda: calls.append("distribute") or "dsum",
        )
        summary = orch.run_once()

        # Fetch was skipped (not failed) — that's a success in pipeline terms.
        assert summary.fetch.skipped_reason is not None
        assert "non-trading day" in summary.fetch.skipped_reason
        assert summary.fetch.succeeded is True
        assert summary.fetch.error_message is None
        # The fetch callable was never invoked.
        assert "fetch" not in calls
        # Compute and distribute still ran.
        assert calls == ["compute", "distribute"]
        assert summary.compute.succeeded
        assert summary.distribute.succeeded

    def test_holiday_treated_same_as_weekend(self):
        # Use an override file that marks today as a holiday.
        import tempfile
        from pathlib import Path
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            # Today is Tuesday 2026-05-19 IST given NOW_MID_SESSION.
            f.write("extra_holidays: [2026-05-19]")
            override_path = Path(f.name)
        try:
            cal = TradingCalendar(TradingCalendarConfig(
                exchange="NSE",
                weekdays=frozenset([1, 2, 3, 4, 5]),
                holidays_override_file=override_path,
            ))
            orch = _build(now=NOW_MID_SESSION, calendar=cal)
            summary = orch.run_once()
            assert summary.fetch.skipped_reason is not None
            assert "non-trading day" in summary.fetch.skipped_reason
        finally:
            override_path.unlink()


# ---------------------------------------------------------------------------
# Step isolation: a failure in one step must not abort the others
# ---------------------------------------------------------------------------


class TestStepIsolation:
    def test_fetch_failure_does_not_abort_compute_and_distribute(self):
        def failing_fetch(ex):
            raise RuntimeError("yfinance is down")

        compute_called = []
        distribute_called = []
        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=failing_fetch,
            compute_fn=lambda: compute_called.append(True),
            distribute_fn=lambda: distribute_called.append(True),
            settings=_settings(fetch_retry_delay_seconds=999),
        )
        summary = orch.run_once()
        assert summary.fetch.succeeded is False
        assert "yfinance is down" in summary.fetch.error_message
        # Compute and distribute still ran.
        assert compute_called == [True]
        assert distribute_called == [True]
        assert summary.compute.succeeded
        assert summary.distribute.succeeded

    def test_compute_failure_does_not_abort_distribute(self):
        def failing_compute():
            raise ValueError("ticker engine blew up")

        distribute_called = []
        orch = _build(
            now=NOW_MID_SESSION,
            compute_fn=failing_compute,
            distribute_fn=lambda: distribute_called.append(True),
        )
        summary = orch.run_once()
        assert summary.fetch.succeeded
        assert summary.compute.succeeded is False
        assert "ticker engine blew up" in summary.compute.error_message
        assert distribute_called == [True]
        assert summary.distribute.succeeded

    def test_distribute_failure_does_not_corrupt_summary(self):
        def failing_distribute():
            raise OSError("smtp unreachable")

        orch = _build(
            now=NOW_MID_SESSION,
            distribute_fn=failing_distribute,
        )
        summary = orch.run_once()
        assert summary.fetch.succeeded
        assert summary.compute.succeeded
        assert summary.distribute.succeeded is False
        assert "smtp unreachable" in summary.distribute.error_message
        # The summary is still complete.
        assert summary.finished_at is not None

    def test_all_three_failing_still_produces_summary(self):
        # Defense-in-depth: even if everything blows up, run_once
        # returns a populated summary and updates last_run_completed_at.
        def raises_a(ex):
            raise RuntimeError("a")
        def raises_b():
            raise RuntimeError("b")
        def raises_c():
            raise RuntimeError("c")

        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=raises_a,
            compute_fn=raises_b,
            distribute_fn=raises_c,
            settings=_settings(fetch_retry_delay_seconds=999),
        )
        summary = orch.run_once()
        assert summary.all_steps_ok is False
        assert "a" in summary.fetch.error_message
        assert "b" in summary.compute.error_message
        assert "c" in summary.distribute.error_message
        assert orch.last_run_completed_at is not None


# ---------------------------------------------------------------------------
# Fetch timeout
# ---------------------------------------------------------------------------


class TestFetchTimeout:
    def test_fetch_exceeding_timeout_recorded_as_failure(self):
        def slow_fetch(ex):
            time.sleep(2.0)
            return "should_not_get_here"

        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=slow_fetch,
            settings=_settings(
                fetch_timeout_seconds=0.1,
                fetch_retry_delay_seconds=999,  # don't fire retry during test
            ),
        )
        t0 = time.monotonic()
        summary = orch.run_once()
        elapsed = time.monotonic() - t0
        # The whole run should complete in well under the slow fetch's
        # 2-second sleep — timeout enforcement worked.
        assert elapsed < 1.0
        assert summary.fetch.succeeded is False
        assert "wall-clock budget" in summary.fetch.error_message

    def test_fetch_within_timeout_succeeds(self):
        # Same shape but fetch completes in time.
        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=lambda ex: (time.sleep(0.02), "rid")[1],
            settings=_settings(fetch_timeout_seconds=1.0),
        )
        summary = orch.run_once()
        assert summary.fetch.succeeded is True
        assert summary.fetch.detail == "rid"


# ---------------------------------------------------------------------------
# Retry on fetch failure
# ---------------------------------------------------------------------------


class TestFetchRetry:
    def test_retry_scheduled_on_first_failure(self):
        def fails(ex):
            raise RuntimeError("flaky")

        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=fails,
            settings=_settings(fetch_retry_delay_seconds=999),
        )
        summary = orch.run_once()
        assert summary.retry_scheduled is True
        assert orch._pending_retry is not None
        # Cleanup: cancel the retry so the executor shuts down cleanly.
        orch._pending_retry.cancel()

    def test_retry_actually_runs_and_can_succeed(self):
        # First call fails, subsequent calls succeed.
        call_count = [0]
        def flaky_fetch(ex):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first attempt fails")
            return "rid_on_retry"

        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=flaky_fetch,
            settings=_settings(fetch_retry_delay_seconds=0.05),
        )
        first_summary = orch.run_once()
        assert first_summary.fetch.succeeded is False
        assert first_summary.retry_scheduled is True

        # Wait for retry to run, capped at 5s.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if call_count[0] >= 2:
                break
            time.sleep(0.05)
        assert call_count[0] >= 2, "retry never fired"

        # Last summary should be from the retry, with fetch=ok.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (orch.last_run_summary
                    and orch.last_run_summary.is_retry
                    and orch.last_run_summary.finished_at is not None):
                break
            time.sleep(0.05)
        assert orch.last_run_summary is not None
        assert orch.last_run_summary.is_retry is True
        assert orch.last_run_summary.fetch.succeeded is True

    def test_retry_does_not_chain_into_another_retry(self):
        # Both first call and retry fail. The retry must NOT schedule
        # yet another retry — that's an infinite chain we explicitly
        # rejected in the design.
        call_count = [0]
        def always_fails(ex):
            call_count[0] += 1
            raise RuntimeError(f"attempt {call_count[0]} fails")

        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=always_fails,
            settings=_settings(fetch_retry_delay_seconds=0.05),
        )
        first_summary = orch.run_once()
        assert first_summary.retry_scheduled is True

        # Wait for retry to complete.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (orch.last_run_summary
                    and orch.last_run_summary.is_retry
                    and orch.last_run_summary.finished_at is not None):
                break
            time.sleep(0.05)

        retry_summary = orch.last_run_summary
        assert retry_summary is not None
        assert retry_summary.is_retry is True
        assert retry_summary.fetch.succeeded is False
        # CRITICAL: the retry did NOT schedule another retry.
        assert retry_summary.retry_scheduled is False
        # And no third call happened.
        time.sleep(0.2)  # give one more retry-delay window a chance
        assert call_count[0] == 2

    def test_double_retry_scheduling_is_coalesced(self):
        # If two concurrent failures hit the schedule path, only one
        # retry should pend (not two).
        def always_fails(ex):
            raise RuntimeError("fail")

        orch = _build(
            now=NOW_MID_SESSION,
            fetch_fn=always_fails,
            settings=_settings(fetch_retry_delay_seconds=999),
        )
        orch._schedule_fetch_retry()
        first_future = orch._pending_retry
        orch._schedule_fetch_retry()
        second_future = orch._pending_retry
        assert first_future is second_future, "second retry stacked on first"
        orch._pending_retry.cancel()


# ---------------------------------------------------------------------------
# Reentrancy
# ---------------------------------------------------------------------------


class TestReentrancy:
    def test_concurrent_run_once_returns_skipped(self):
        # Hold the lock manually, then call run_once and verify it's
        # skipped without running any step.
        compute_count = [0]
        orch = _build(
            now=NOW_MID_SESSION,
            compute_fn=lambda: compute_count.__setitem__(0, compute_count[0] + 1),
        )
        assert orch._run_lock.acquire(blocking=False) is True
        try:
            summary = orch.run_once()
            assert summary.skipped_due_to_concurrent_run is True
            # None of the pipeline ran.
            assert compute_count[0] == 0
            # Nothing was written to last_run_completed_at since the
            # skipped pass never finished a real run.
            assert orch.last_run_completed_at is None
        finally:
            orch._run_lock.release()

    def test_sequential_run_once_calls_each_acquire_lock_cleanly(self):
        orch = _build(now=NOW_MID_SESSION)
        s1 = orch.run_once()
        s2 = orch.run_once()
        assert not s1.skipped_due_to_concurrent_run
        assert not s2.skipped_due_to_concurrent_run
        # Different correlation IDs.
        assert s1.correlation_id != s2.correlation_id


# ---------------------------------------------------------------------------
# Pulse publisher
# ---------------------------------------------------------------------------


class TestPulsePublisher:
    def test_pulse_publish_called_on_success(self):
        pulse = MagicMock()
        orch = _build(now=NOW_MID_SESSION, pulse=pulse)
        summary = orch.run_once()
        pulse.publish.assert_called_once_with(summary)

    def test_pulse_publish_called_even_when_steps_fail(self):
        pulse = MagicMock()
        def failing_compute():
            raise RuntimeError("x")
        orch = _build(
            now=NOW_MID_SESSION,
            pulse=pulse,
            compute_fn=failing_compute,
        )
        summary = orch.run_once()
        # Pulse still gets the (failed-step) summary so the GUI knows
        # something happened.
        pulse.publish.assert_called_once_with(summary)

    def test_pulse_exception_does_not_break_orchestrator(self):
        pulse = MagicMock()
        pulse.publish.side_effect = RuntimeError("subscriber buffer full")
        orch = _build(now=NOW_MID_SESSION, pulse=pulse)
        # Must not raise.
        summary = orch.run_once()
        assert summary.all_steps_ok  # pipeline itself succeeded
        # And the orchestrator's bookkeeping is intact.
        assert orch.last_run_completed_at is not None

    def test_no_pulse_publisher_is_fine(self):
        orch = _build(now=NOW_MID_SESSION, pulse=None)
        # Doesn't raise.
        summary = orch.run_once()
        assert summary.all_steps_ok


# ---------------------------------------------------------------------------
# Catch-up on startup
# ---------------------------------------------------------------------------


class TestCatchUpOnStartup:
    def test_catch_up_skipped_if_recent(self):
        now = datetime(2026, 5, 19, 6, 0, 0, tzinfo=UTC)
        orch = _build(
            now=now,
            settings=_settings(
                catch_up_on_startup=True,
                catch_up_skip_if_recent_seconds=30 * 60,
            ),
        )
        # Last run 5 minutes ago — within the skip window.
        orch.last_run_completed_at = now - timedelta(minutes=5)
        assert orch._should_catch_up_now() is False

    def test_catch_up_fires_if_last_run_was_long_ago(self):
        now = datetime(2026, 5, 19, 6, 0, 0, tzinfo=UTC)
        orch = _build(
            now=now,
            settings=_settings(
                catch_up_on_startup=True,
                catch_up_skip_if_recent_seconds=30 * 60,
            ),
        )
        # Last run 2 hours ago — well outside skip window.
        orch.last_run_completed_at = now - timedelta(hours=2)
        assert orch._should_catch_up_now() is True

    def test_catch_up_fires_when_no_prior_run(self):
        now = datetime(2026, 5, 19, 6, 0, 0, tzinfo=UTC)
        orch = _build(
            now=now,
            settings=_settings(catch_up_on_startup=True),
        )
        # No prior run.
        assert orch.last_run_completed_at is None
        assert orch._should_catch_up_now() is True

    def test_catch_up_not_fired_before_first_tick_of_day(self):
        # 08:00 IST = 02:30 UTC, before first tick (09:20 IST = 03:50 UTC).
        now = datetime(2026, 5, 19, 2, 30, 0, tzinfo=UTC)
        orch = _build(
            now=now,
            settings=_settings(catch_up_on_startup=True),
        )
        # We're before any tick today; nothing to catch up.
        assert orch._should_catch_up_now() is False

    def test_catch_up_not_fired_after_last_tick_of_day(self):
        # 16:00 IST = 10:30 UTC, after the 15:15 IST last tick (09:45 UTC).
        now = datetime(2026, 5, 19, 10, 30, 0, tzinfo=UTC)
        orch = _build(
            now=now,
            settings=_settings(catch_up_on_startup=True),
        )
        # All today's ticks have passed; no point catching up the past.
        assert orch._should_catch_up_now() is False

    def test_catch_up_not_fired_on_non_trading_day(self):
        orch = _build(
            now=NOW_SATURDAY,
            settings=_settings(catch_up_on_startup=True),
        )
        assert orch._should_catch_up_now() is False


# ---------------------------------------------------------------------------
# Lifecycle: start/stop/is_alive
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_is_alive_false_before_start(self):
        orch = _build()
        assert orch.is_alive() is False
        # Never started → not "unhealthy"; just inactive.
        assert orch.is_unhealthy() is False

    def test_start_then_stop_cleanly(self):
        orch = _build()
        orch.start()
        assert orch.is_alive() is True
        assert orch.is_unhealthy() is False
        orch.stop(timeout=2.0)
        assert orch.is_alive() is False
        # After stop, was_started is True but loop_thread is None → unhealthy.
        # That's correct: a stopped-but-once-started orchestrator IS
        # unhealthy from /readyz's perspective. Operator should restart it.
        assert orch.is_unhealthy() is True

    def test_double_start_is_noop(self):
        orch = _build()
        orch.start()
        # Should log a warning but not crash or spawn a second thread.
        orch.start()
        try:
            assert orch.is_alive() is True
        finally:
            orch.stop(timeout=2.0)

    def test_stop_without_start_safe(self):
        orch = _build()
        # Calling stop on an orchestrator that was never started is safe.
        orch.stop(timeout=1.0)
        assert orch.is_alive() is False


# ---------------------------------------------------------------------------
# Summary type basics
# ---------------------------------------------------------------------------


class TestSummaryTypes:
    def test_step_result_defaults(self):
        r = OrchestratorStepResult()
        assert r.succeeded is False
        assert r.skipped_reason is None
        assert r.error_message is None
        assert r.duration_seconds == 0.0
        assert r.detail is None

    def test_all_steps_ok_requires_all_three(self):
        s = OrchestratorRunSummary(exchange="NSE", started_at=NOW_MID_SESSION)
        assert s.all_steps_ok is False
        s.fetch.succeeded = True
        s.compute.succeeded = True
        s.distribute.succeeded = True
        assert s.all_steps_ok is True

    def test_total_duration_zero_when_not_finished(self):
        s = OrchestratorRunSummary(exchange="NSE", started_at=NOW_MID_SESSION)
        assert s.total_duration_seconds == 0.0
