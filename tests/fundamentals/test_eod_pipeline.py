"""Tests for the Phase 4C EoD pipeline orchestrator.

The orchestrator delegates to the per-phase runners which have
their own coverage. These tests focus on the orchestration logic:
step ordering, failure propagation, opt-in tickers flag, progress
callback, and result-struct shape.

Most heavy lifting is monkey-patched out; the goal isn't to run
60-second integrations but to verify the orchestrator wires
everything correctly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from trip_ema.fundamentals.eod_pipeline import (
    PipelineResult,
    StepResult,
    StepStatus,
    _status_from_counts,
    _status_from_run,
    run_eod_pipeline,
)


# ---------- helpers ---------- #


class _FakeRun:
    """Stand-in for a FundamentalsRun row."""
    def __init__(self, status="SUCCESS", succeeded=10, failed=0, skipped=0):
        self.status = status
        self.succeeded = succeeded
        self.failed = failed
        self.skipped = skipped


def _patch_all_runners(
    monkeypatch,
    *,
    industry_status="SUCCESS",
    shares_status="SUCCESS",
    financials_status="SUCCESS",
    market_cap_succeeded=100,
    index_build_report=None,
    index_value_status="SUCCESS",
    indicator_industries_report=None,
    signals_industries_report=None,
    indicator_tickers_report=None,
    signals_tickers_report=None,
):
    """One-stop shop to patch all 10 runners with controllable
    outcomes. Returns the dict of MagicMock objects so individual
    tests can assert call patterns."""
    if index_build_report is None:
        index_build_report = {
            "created": [],
            "updated": ["IT - Services", "Banks - Regional"],
            "skipped": [],
            "failed": [],
        }
    if indicator_industries_report is None:
        indicator_industries_report = {
            "succeeded": [("IT - Services", 152, 6)],
            "skipped": [],
            "failed": [],
        }
    if signals_industries_report is None:
        signals_industries_report = {
            "succeeded": [("IT - Services", 17)],
            "skipped": [],
            "failed": [],
        }
    if indicator_tickers_report is None:
        indicator_tickers_report = {
            "succeeded": [(1, "RELIANCE", 80, 6)],
            "skipped": [],
            "failed": [],
        }
    if signals_tickers_report is None:
        signals_tickers_report = {
            "succeeded": [(1, "RELIANCE", 4)],
            "skipped": [],
            "failed": [],
        }

    mocks = {}

    # Phase 1-3 (yfinance) — return a run_id, the orchestrator
    # then reads the row's status.
    def make_run_id_fn(status, succeeded=100, failed=0, skipped=0):
        def _fn(*args, **kwargs):
            return 42  # arbitrary run_id
        return _fn

    mocks["industry"] = MagicMock(side_effect=make_run_id_fn(industry_status))
    mocks["shares"] = MagicMock(side_effect=make_run_id_fn(shares_status))
    mocks["financials"] = MagicMock(
        side_effect=make_run_id_fn(financials_status)
    )

    monkeypatch.setattr(
        "trip_ema.fundamentals.eod_pipeline.run_industry_refresh"
        if False else "trip_ema.fundamentals.runners.run_industry_refresh",
        mocks["industry"],
    )
    monkeypatch.setattr(
        "trip_ema.fundamentals.runners.run_shares_refresh",
        mocks["shares"],
    )
    monkeypatch.setattr(
        "trip_ema.fundamentals.runners.run_financials_refresh",
        mocks["financials"],
    )

    # Patch FundamentalsRunRepository.get so the orchestrator reads
    # the right status for each run_id.
    runs_by_status = {
        # ID 42 returned by all three; orchestrator calls .get() once
        # per phase in succession. We need a side_effect that returns
        # the right status depending on call order. Use a closure.
        "industry": industry_status,
        "shares": shares_status,
        "financials": financials_status,
    }
    call_log = []

    def fake_get(self, rid):
        # The orchestrator only calls .get(run_id) immediately after
        # the runner returns; statuses unfold in the same order as
        # the phases. We can't easily map run_id→status because they
        # all return 42. Track call count instead.
        call_log.append(rid)
        n = len(call_log)
        if n == 1:
            return _FakeRun(status=industry_status, succeeded=100)
        if n == 2:
            return _FakeRun(status=shares_status, succeeded=100)
        if n == 3:
            return _FakeRun(status=financials_status, succeeded=100)
        # Beyond that — market_cap and index_values open their own
        # run_ids — we just return a default success.
        return _FakeRun(status="SUCCESS", succeeded=50)
    from trip_ema.persistence.fundamentals_repo import (
        FundamentalsRunRepository,
    )
    monkeypatch.setattr(FundamentalsRunRepository, "get", fake_get)

    # Phase 4A — market cap. The orchestrator calls
    # compute_market_cap_for_date directly.
    from trip_ema.fundamentals.market_cap_compute import ComputeResult
    from datetime import date
    fake_mc_result = ComputeResult(
        trade_date=date.today(),
        exchange="NSE",
        succeeded_computed=market_cap_succeeded,
        succeeded_approx=0,
        skipped_no_close=5,
        skipped_no_shares=3,
    )
    mocks["market_cap"] = MagicMock(return_value=fake_mc_result)
    monkeypatch.setattr(
        "trip_ema.fundamentals.market_cap_compute.compute_market_cap_for_date",
        mocks["market_cap"],
    )

    # Phase 4B — index build (returns dict).
    mocks["index_build"] = MagicMock(return_value=index_build_report)
    monkeypatch.setattr(
        "trip_ema.fundamentals.runners.run_industry_index_build",
        mocks["index_build"],
    )

    # Phase 4B — index value backfill (returns run_id, looped).
    mocks["index_values"] = MagicMock(return_value=43)
    monkeypatch.setattr(
        "trip_ema.fundamentals.runners.run_index_value_backfill",
        mocks["index_values"],
    )

    # Phase 5 — indicator state (industries).
    mocks["indicator_industries"] = MagicMock(
        return_value=indicator_industries_report
    )
    monkeypatch.setattr(
        "trip_ema.fundamentals.runners.run_indicator_state_for_all_industries",
        mocks["indicator_industries"],
    )

    # Phase 6 — signals (industries).
    mocks["signals_industries"] = MagicMock(
        return_value=signals_industries_report
    )
    monkeypatch.setattr(
        "trip_ema.fundamentals.runners.run_signal_events_for_all_industries",
        mocks["signals_industries"],
    )

    # Phase 7 — per-ticker indicator + signals.
    mocks["indicator_tickers"] = MagicMock(
        return_value=indicator_tickers_report
    )
    monkeypatch.setattr(
        "trip_ema.fundamentals.runners.run_indicator_state_for_all_tickers",
        mocks["indicator_tickers"],
    )
    mocks["signals_tickers"] = MagicMock(
        return_value=signals_tickers_report
    )
    monkeypatch.setattr(
        "trip_ema.fundamentals.runners.run_signal_events_for_all_tickers",
        mocks["signals_tickers"],
    )

    # Patch IndustryIndexRepository.list_active to return a small list
    # for the index_values step.
    class _FakeIdx:
        def __init__(self, name):
            self.industry_name = name
            from datetime import date as _date
            self.base_date = _date(2025, 10, 1)
    fake_indices = [_FakeIdx("IT - Services"), _FakeIdx("Banks - Regional")]
    from trip_ema.persistence.fundamentals_repo import IndustryIndexRepository
    monkeypatch.setattr(
        IndustryIndexRepository, "list_active",
        lambda self: fake_indices,
    )

    return mocks


# ============================================================
# Basic orchestration
# ============================================================


class TestOrchestration:

    def test_all_steps_run_in_order_default(self, db_engine, monkeypatch):
        """Without include_tickers, exactly 8 steps run in the
        documented order."""
        _patch_all_runners(monkeypatch)
        result = run_eod_pipeline(db_engine, "NSE")
        step_names = [s.name for s in result.steps]
        assert step_names == [
            "refresh-industry",
            "refresh-shares",
            "refresh-financials",
            "compute-market-cap",
            "build-industry-indices",
            "compute-index-values",
            "compute-indicator-state (industries)",
            "compute-signals (industries)",
        ]

    def test_include_tickers_adds_two_steps(self, db_engine, monkeypatch):
        """include_tickers=True appends 2 per-ticker steps at the end."""
        _patch_all_runners(monkeypatch)
        result = run_eod_pipeline(
            db_engine, "NSE", include_tickers=True, timeframe="1W",
        )
        step_names = [s.name for s in result.steps]
        assert len(step_names) == 10
        assert step_names[-2] == "compute-indicator-state (tickers)"
        assert step_names[-1] == "compute-signals (tickers)"

    def test_all_success_yields_success_overall(self, db_engine, monkeypatch):
        _patch_all_runners(monkeypatch)
        result = run_eod_pipeline(db_engine, "NSE")
        assert result.overall_status == "SUCCESS"
        for s in result.steps:
            assert s.status == StepStatus.SUCCESS

    def test_result_has_timing(self, db_engine, monkeypatch):
        _patch_all_runners(monkeypatch)
        result = run_eod_pipeline(db_engine, "NSE")
        assert result.started_at is not None
        assert result.finished_at is not None
        assert result.duration_seconds is not None
        assert result.duration_seconds >= 0
        for s in result.steps:
            assert s.started_at is not None
            assert s.finished_at is not None

    def test_exchange_propagated_to_runners(self, db_engine, monkeypatch):
        mocks = _patch_all_runners(monkeypatch)
        run_eod_pipeline(db_engine, "BSE")
        # Phases 1-3 receive exchange as the second positional arg.
        assert mocks["industry"].call_args[0][1] == "BSE"
        assert mocks["shares"].call_args[0][1] == "BSE"
        assert mocks["financials"].call_args[0][1] == "BSE"
        # Phase 4A and 4B too.
        assert mocks["market_cap"].call_args[0][1] == "BSE"
        assert mocks["index_build"].call_args[0][1] == "BSE"

    def test_force_propagated(self, db_engine, monkeypatch):
        mocks = _patch_all_runners(monkeypatch)
        run_eod_pipeline(db_engine, "NSE", force=True)
        assert mocks["industry"].call_args.kwargs.get("force") is True
        assert mocks["shares"].call_args.kwargs.get("force") is True
        assert mocks["financials"].call_args.kwargs.get("force") is True


# ============================================================
# Failure handling
# ============================================================


class TestFailurePropagation:

    def test_fatal_step_aborts_remaining(self, db_engine, monkeypatch):
        """If refresh-industry fails fatally, subsequent steps are
        marked NOT_RUN."""
        _patch_all_runners(monkeypatch, industry_status="FAILED")
        result = run_eod_pipeline(db_engine, "NSE")
        assert result.steps[0].name == "refresh-industry"
        assert result.steps[0].status == StepStatus.FAILED
        for s in result.steps[1:]:
            assert s.status == StepStatus.NOT_RUN
        assert result.overall_status == "FAILED"

    def test_partial_step_does_not_abort(self, db_engine, monkeypatch):
        """A PARTIAL step continues to the next step. Overall status
        becomes PARTIAL."""
        _patch_all_runners(monkeypatch, industry_status="PARTIAL")
        result = run_eod_pipeline(db_engine, "NSE")
        assert result.steps[0].status == StepStatus.PARTIAL
        # Subsequent steps still ran.
        for s in result.steps[1:]:
            assert s.status != StepStatus.NOT_RUN
        assert result.overall_status == "PARTIAL"

    def test_exception_in_step_is_caught(self, db_engine, monkeypatch):
        """A raised exception is caught, the step is marked FAILED
        with the error message, and remaining steps don't run."""
        _patch_all_runners(monkeypatch)
        # Make the shares step raise.
        from trip_ema.fundamentals import runners
        def boom(*a, **kw):
            raise RuntimeError("simulated crash")
        monkeypatch.setattr(runners, "run_shares_refresh", boom)
        result = run_eod_pipeline(db_engine, "NSE")
        # Step 0 still ran.
        assert result.steps[0].status == StepStatus.SUCCESS
        # Step 1 (shares) crashed.
        assert result.steps[1].name == "refresh-shares"
        assert result.steps[1].status == StepStatus.FAILED
        assert "simulated crash" in (result.steps[1].error or "")
        # Step 2+ NOT_RUN.
        for s in result.steps[2:]:
            assert s.status == StepStatus.NOT_RUN

    def test_per_unit_failures_become_partial(
        self, db_engine, monkeypatch,
    ):
        """If a bulk step reports some failed units, the step is
        PARTIAL (not FAILED)."""
        report = {
            "succeeded": [("IT", 152, 6)],
            "skipped": [],
            "failed": [("Bad Industry", "error msg")],
        }
        _patch_all_runners(
            monkeypatch, indicator_industries_report=report,
        )
        result = run_eod_pipeline(db_engine, "NSE")
        ind_step = next(
            s for s in result.steps
            if s.name == "compute-indicator-state (industries)"
        )
        assert ind_step.status == StepStatus.PARTIAL
        assert ind_step.failed == 1


# ============================================================
# Progress callback
# ============================================================


class TestProgressCallback:

    def test_callback_invoked_per_step(self, db_engine, monkeypatch):
        _patch_all_runners(monkeypatch)
        calls = []
        def cb(step_result):
            calls.append((step_result.name, step_result.status))
        result = run_eod_pipeline(
            db_engine, "NSE", progress_callback=cb,
        )
        assert len(calls) == len(result.steps)
        for (name, status) in calls:
            assert isinstance(status, StepStatus)

    def test_callback_invoked_for_not_run_steps(
        self, db_engine, monkeypatch,
    ):
        """After a fatal failure, the callback is still invoked
        for the NOT_RUN steps so callers can show 'didn't run'
        feedback."""
        _patch_all_runners(monkeypatch, industry_status="FAILED")
        calls = []
        def cb(step_result):
            calls.append(step_result.status)
        run_eod_pipeline(db_engine, "NSE", progress_callback=cb)
        # 1 FAILED + 7 NOT_RUN.
        assert calls[0] == StepStatus.FAILED
        for s in calls[1:]:
            assert s == StepStatus.NOT_RUN


# ============================================================
# Helper functions
# ============================================================


class TestStatusFromCounts:

    def test_all_success(self):
        assert _status_from_counts(10, 0, 0) == StepStatus.SUCCESS

    def test_partial_when_some_failed(self):
        assert _status_from_counts(8, 2, 0) == StepStatus.PARTIAL

    def test_failed_when_all_failed(self):
        assert _status_from_counts(0, 5, 0) == StepStatus.FAILED

    def test_success_with_skipped(self):
        assert _status_from_counts(5, 0, 3) == StepStatus.SUCCESS


class TestStatusFromRun:

    def test_none_run_is_failed(self):
        assert _status_from_run(None) == StepStatus.FAILED

    def test_success_status(self):
        assert _status_from_run(_FakeRun("SUCCESS")) == StepStatus.SUCCESS

    def test_partial_status(self):
        assert _status_from_run(_FakeRun("PARTIAL")) == StepStatus.PARTIAL

    def test_failed_status(self):
        assert _status_from_run(_FakeRun("FAILED")) == StepStatus.FAILED

    def test_unknown_status_treated_as_failed(self):
        assert _status_from_run(_FakeRun("BOGUS")) == StepStatus.FAILED


# ============================================================
# Result struct
# ============================================================


class TestPipelineResultStruct:

    def test_step_result_duration_is_none_before_run(self):
        sr = StepResult(name="x")
        assert sr.duration_seconds is None

    def test_step_result_duration_populated_after_run(self):
        sr = StepResult(name="x")
        sr.started_at = datetime.now(timezone.utc)
        sr.finished_at = sr.started_at
        assert sr.duration_seconds == 0

    def test_pipeline_result_overall_status_logic(self):
        from datetime import datetime, timezone
        pr = PipelineResult(
            exchange="NSE",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            steps=[
                StepResult(name="a", status=StepStatus.SUCCESS),
                StepResult(name="b", status=StepStatus.SUCCESS),
            ],
        )
        assert pr.overall_status == "SUCCESS"
        pr.steps[1].status = StepStatus.PARTIAL
        assert pr.overall_status == "PARTIAL"
        pr.steps[1].status = StepStatus.FAILED
        assert pr.overall_status == "FAILED"

    def test_partial_via_unit_failures(self):
        from datetime import datetime, timezone
        pr = PipelineResult(
            exchange="NSE",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            steps=[
                StepResult(
                    name="a", status=StepStatus.SUCCESS,
                    succeeded=10, failed=2,
                ),
            ],
        )
        # Step status is SUCCESS but there were unit failures →
        # overall is PARTIAL.
        assert pr.overall_status == "PARTIAL"


# ============================================================
# Bug 1: build-industry-indices steady-state outcomes
# ============================================================


class TestIndexBuildSteadyState:
    """Daily-run scenarios for the index-build step. The fleet is
    already built; only marginal industries (sub-threshold
    constituents) appear in the return dict. This must not abort
    the pipeline."""

    def test_no_new_work_yields_success(self, db_engine, monkeypatch):
        """Steady state: nothing new to index, nothing failed.
        Status SUCCESS, pipeline continues."""
        # Empty report — everything that could be indexed already is.
        empty_build_report = {
            "qualified": [("IT - Services", "Tech", 25),
                          ("Banks - Regional", "Fin", 18)],
            "unqualified": [],
            "indexed": [],
            "failed": [],
        }
        _patch_all_runners(
            monkeypatch, index_build_report=empty_build_report,
        )
        result = run_eod_pipeline(db_engine, "NSE")
        build_step = next(
            s for s in result.steps if s.name == "build-industry-indices"
        )
        assert build_step.status == StepStatus.SUCCESS
        assert build_step.succeeded == 0
        assert build_step.failed == 0
        # Downstream steps still ran.
        downstream = result.steps[result.steps.index(build_step) + 1:]
        for s in downstream:
            assert s.status != StepStatus.NOT_RUN

    def test_sub_threshold_failures_are_partial_not_fatal(
        self, db_engine, monkeypatch,
    ):
        """When ALL marginal industries can't pick enough
        constituents and NO new industries are indexed, the step
        is PARTIAL (not FAILED). Downstream steps continue with
        the existing fleet."""
        report = {
            "qualified": [("IT - Services", "Tech", 25)],
            "unqualified": [("Restaurants", "Consumer", 8)],
            "indexed": [],  # no new work — existing fleet stayed
            "failed": [
                ("Other Industrial Metals & Mining", "picked 8 < min 10"),
                ("Restaurants", "picked 9 < min 10"),
                ("Tools & Accessories", "picked 8 < min 10"),
            ],
        }
        _patch_all_runners(monkeypatch, index_build_report=report)
        result = run_eod_pipeline(db_engine, "NSE")
        build_step = next(
            s for s in result.steps if s.name == "build-industry-indices"
        )
        # NOT FAILED — the existing fleet is intact.
        assert build_step.status == StepStatus.PARTIAL
        assert build_step.failed == 3
        # All downstream steps ran.
        downstream = result.steps[result.steps.index(build_step) + 1:]
        for s in downstream:
            assert s.status != StepStatus.NOT_RUN

    def test_partial_index_build_propagates_to_overall_partial(
        self, db_engine, monkeypatch,
    ):
        """Overall pipeline status must reflect the build step's
        PARTIAL outcome."""
        report = {
            "qualified": [],
            "unqualified": [],
            "indexed": [],
            "failed": [("X", "err")],
        }
        _patch_all_runners(monkeypatch, index_build_report=report)
        result = run_eod_pipeline(db_engine, "NSE")
        # Overall status: PARTIAL (no FAILED but one step PARTIAL).
        assert result.overall_status == "PARTIAL"

    def test_mixed_new_and_failed_is_partial(self, db_engine, monkeypatch):
        """Newly indexed AND some failed → PARTIAL."""
        from datetime import date as _date
        report = {
            "qualified": [("New Industry", "Sec", 12),
                          ("Other Marginal", "Sec", 9)],
            "unqualified": [],
            "indexed": [("New Industry", _date(2025, 10, 1), 10)],
            "failed": [("Other Marginal", "picked 8 < 10")],
        }
        _patch_all_runners(monkeypatch, index_build_report=report)
        result = run_eod_pipeline(db_engine, "NSE")
        build_step = next(
            s for s in result.steps if s.name == "build-industry-indices"
        )
        assert build_step.status == StepStatus.PARTIAL
        assert build_step.succeeded == 1
        assert build_step.failed == 1


# ============================================================
# Bug 2: market-cap no-trading-day handling
# ============================================================


class TestMarketCapNoTradingDay:

    def test_weekend_yields_skipped_step(self, db_engine, monkeypatch):
        """When NO ticker has a close for today (market closed),
        emit SKIPPED status and continue to subsequent steps."""
        from trip_ema.fundamentals.market_cap_compute import ComputeResult
        from datetime import date as _date
        no_data_result = ComputeResult(
            trade_date=_date.today(),
            exchange="NSE",
            succeeded_computed=0,
            succeeded_approx=0,
            skipped_no_close=2347,  # all tickers — market was closed
            skipped_no_shares=0,
        )
        _patch_all_runners(monkeypatch)
        # Override just the market_cap call.
        monkeypatch.setattr(
            "trip_ema.fundamentals.market_cap_compute.compute_market_cap_for_date",
            lambda engine, exchange, td: no_data_result,
        )
        result = run_eod_pipeline(db_engine, "NSE")
        mc_step = next(
            s for s in result.steps if s.name == "compute-market-cap"
        )
        assert mc_step.status == StepStatus.SKIPPED
        assert "market was closed" in mc_step.notes
        # Downstream steps still ran (they'll see no new mcap data
        # but that's their own concern).
        downstream = result.steps[result.steps.index(mc_step) + 1:]
        for s in downstream:
            assert s.status != StepStatus.NOT_RUN

    def test_partial_data_still_yields_success(self, db_engine, monkeypatch):
        """If some tickers computed and others were missing
        (normal partial-data scenario, not a closed-market day),
        status is SUCCESS."""
        from trip_ema.fundamentals.market_cap_compute import ComputeResult
        from datetime import date as _date
        partial_result = ComputeResult(
            trade_date=_date.today(),
            exchange="NSE",
            succeeded_computed=2000,
            succeeded_approx=50,
            skipped_no_close=100,
            skipped_no_shares=50,
        )
        _patch_all_runners(monkeypatch)
        monkeypatch.setattr(
            "trip_ema.fundamentals.market_cap_compute.compute_market_cap_for_date",
            lambda engine, exchange, td: partial_result,
        )
        result = run_eod_pipeline(db_engine, "NSE")
        mc_step = next(
            s for s in result.steps if s.name == "compute-market-cap"
        )
        # 2050 succeeded — that's a healthy run, not a closed-market.
        assert mc_step.status == StepStatus.SUCCESS

    def test_no_close_and_no_shares_yields_partial(
        self, db_engine, monkeypatch,
    ):
        """Weird case: bars exist for some tickers (so market WAS
        open) but shares data is missing → PARTIAL, not SKIPPED."""
        from trip_ema.fundamentals.market_cap_compute import ComputeResult
        from datetime import date as _date
        odd_result = ComputeResult(
            trade_date=_date.today(),
            exchange="NSE",
            succeeded_computed=0,
            succeeded_approx=0,
            skipped_no_close=100,
            skipped_no_shares=2200,  # most tickers have bars but no shares
        )
        _patch_all_runners(monkeypatch)
        monkeypatch.setattr(
            "trip_ema.fundamentals.market_cap_compute.compute_market_cap_for_date",
            lambda engine, exchange, td: odd_result,
        )
        result = run_eod_pipeline(db_engine, "NSE")
        mc_step = next(
            s for s in result.steps if s.name == "compute-market-cap"
        )
        # Not a closed-market case (shares are missing, not bars).
        assert mc_step.status == StepStatus.PARTIAL
        assert "market was closed" not in mc_step.notes
