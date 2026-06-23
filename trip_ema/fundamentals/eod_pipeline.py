"""Phase 4C — EoD pipeline orchestrator.

Wires the 8 fundamentals phases (refresh-industry → refresh-shares →
refresh-financials → compute-market-cap → build-industry-indices →
compute-index-values → compute-indicator-state → compute-signals)
as a single pipeline.

Operator-facing entry point: `run_eod_pipeline()`. The CLI command
`run-eod-pipeline` is a thin wrapper around this; the bars-ingestion
daemon's EoD hook can also call it directly without subprocess.

Failure model:
  - **Step succeeded**: continue to next step.
  - **Step PARTIAL** (e.g. some tickers failed in refresh-industry,
    but most succeeded): continue to next step. The overall pipeline
    result records the partial state.
  - **Step FAILED** (no work succeeded, e.g. DB connection lost):
    abort the pipeline. Subsequent steps are marked "not run".

Resumability:
  - Each step is internally resumable (the per-phase runners have
    their own skip-if-recent guards and checkpoint patterns).
  - The pipeline itself doesn't checkpoint between steps. If a step
    fails fatally, the operator re-runs the pipeline; the steps that
    already succeeded will skip-if-recent.

Per-ticker compute (Phase 7) is **part of the standard pipeline
order** as of Phase 6-2. Order per the operator's specification:
  1. fetch stock data (industry / shares / financials / market cap)
  2. calculate EMAs per stock
  3. calculate and store signals per stock
  4. calculate industry_index (build + values)
  5. calculate industry EMAs
  6. calculate industry signals (regions)

The include_tickers flag remains for callers that explicitly want
to skip the per-stock phases (e.g. an ad-hoc industries-only re-run
after changing the precedence logic). The CLI defaults to True so
the operator's pipeline runs end-to-end without an extra flag.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from sqlalchemy.engine import Engine


logger = logging.getLogger(__name__)


class StepStatus(str, Enum):
    """Outcome of a single pipeline step."""
    PENDING = "PENDING"      # not yet run
    RUNNING = "RUNNING"      # currently executing
    SUCCESS = "SUCCESS"      # ran cleanly, all units succeeded
    PARTIAL = "PARTIAL"      # ran but some units failed/skipped
    FAILED = "FAILED"        # fatal — couldn't make progress
    SKIPPED = "SKIPPED"      # was opt-in and not requested
    NOT_RUN = "NOT_RUN"      # earlier step failed, so this didn't run


@dataclass
class StepResult:
    """Outcome record for one pipeline step."""
    name: str
    status: StepStatus = StepStatus.PENDING
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    notes: str = ""
    error: Optional[str] = None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


@dataclass
class PipelineResult:
    """Outcome of a full EoD pipeline run."""
    exchange: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    steps: list[StepResult] = field(default_factory=list)
    include_tickers: bool = False
    timeframe: Optional[str] = None  # for per-ticker steps

    @property
    def overall_status(self) -> str:
        """Roll up the step statuses into a single overall verdict.

        - 'FAILED' if any step failed fatally (and the rest are NOT_RUN).
        - 'PARTIAL' if any step is PARTIAL or any unit failed.
        - 'SUCCESS' if all relevant steps succeeded.
        """
        if any(s.status == StepStatus.FAILED for s in self.steps):
            return "FAILED"
        if any(s.status == StepStatus.PARTIAL for s in self.steps):
            return "PARTIAL"
        if any(s.failed > 0 for s in self.steps):
            return "PARTIAL"
        return "SUCCESS"

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


def run_eod_pipeline(
    db_engine: Engine,
    exchange: str,
    *,
    include_tickers: bool = True,
    timeframe: str = "1W",
    force: bool = False,
    progress_callback: Optional[Callable[[StepResult], None]] = None,
) -> PipelineResult:
    """Run the full EoD fundamentals pipeline for one exchange.

    Args:
      db_engine: SQLAlchemy engine.
      exchange: 'NSE' or 'BSE'. Per-exchange steps run only for this
        exchange; cross-exchange steps (compute-market-cap, index
        values, indicator state, signals) process all data.
      include_tickers: if False, skip the per-stock indicator and
        signal compute steps. Default True so the canonical
        per-stock → per-industry pipeline runs end-to-end. Set False
        only when you want an industries-only run.
      timeframe: timeframe for per-ticker compute (only used if
        include_tickers=True). Default '1W'.
      force: pass force=True to each step's runner, bypassing all
        skip-if-recent guards. Use with caution — re-runs full
        history compute.
      progress_callback: optional callable invoked after each step
        completes with the StepResult. Useful for live progress
        display in a CLI or HTTP endpoint.

    Returns:
      PipelineResult with per-step outcomes and overall status.
    """
    from .runners import (
        run_financials_refresh,
        run_indicator_state_for_all_industries,
        run_indicator_state_for_all_tickers,
        run_industry_index_build,
        run_industry_refresh,
        run_index_value_backfill,
        run_shares_refresh,
        run_signal_events_for_all_tickers,
    )
    from .benchmark_stats_runners import (
        run_benchmark_stats_for_all_industries,
        run_benchmark_stats_for_all_tickers,
    )

    result = PipelineResult(
        exchange=exchange,
        started_at=datetime.now(timezone.utc),
        include_tickers=include_tickers,
        timeframe=timeframe if include_tickers else None,
    )

    # Define the pipeline steps. Each step is (name, callable).
    # The callable takes no args (we close over db_engine + flags)
    # and returns a tuple (status, succeeded, failed, skipped, notes).
    #
    # The fact that some runners return a run_id while others return
    # a dict is normalized inside each closure.

    def _step_industry_refresh():
        run_id = run_industry_refresh(db_engine, exchange, force=force)
        with db_engine.connect() as conn:
            from ..persistence.fundamentals_repo import (
                FundamentalsRunRepository,
            )
            r = FundamentalsRunRepository(conn).get(run_id)
        s = _status_from_run(r)
        return (s, r.succeeded, r.failed, r.skipped,
                f"refresh-industry on {exchange}")

    def _step_shares_refresh():
        run_id = run_shares_refresh(db_engine, exchange, force=force)
        with db_engine.connect() as conn:
            from ..persistence.fundamentals_repo import (
                FundamentalsRunRepository,
            )
            r = FundamentalsRunRepository(conn).get(run_id)
        s = _status_from_run(r)
        return (s, r.succeeded, r.failed, r.skipped,
                f"refresh-shares on {exchange}")

    def _step_financials_refresh():
        run_id = run_financials_refresh(db_engine, exchange, force=force)
        with db_engine.connect() as conn:
            from ..persistence.fundamentals_repo import (
                FundamentalsRunRepository,
            )
            r = FundamentalsRunRepository(conn).get(run_id)
        s = _status_from_run(r)
        return (s, r.succeeded, r.failed, r.skipped,
                f"refresh-financials on {exchange}")

    def _step_market_cap():
        # EoD market cap: single-day path (no yfinance) — fast,
        # uses local ohlcv_bars × instrument_shares JOIN.
        # The backfill variant is for catching up multi-day gaps;
        # the daily pipeline always operates on today.
        from datetime import date as _date
        from .market_cap_compute import compute_market_cap_for_date
        from ..persistence.fundamentals_repo import (
            FundamentalsRunRepository,
        )
        from ..persistence import transaction
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase="market_cap", exchange=exchange,
            )
        try:
            cr = compute_market_cap_for_date(
                db_engine, exchange, _date.today(),
            )
            succeeded = cr.succeeded_computed + cr.succeeded_approx
            skipped = cr.skipped_no_close + cr.skipped_no_shares
            failed = 0

            # No-trading-day detection: every ticker was skipped
            # for "no close" → no bars exist for today → market
            # was closed (weekend or holiday).
            no_trading_day = (
                succeeded == 0
                and cr.skipped_no_close > 0
                and cr.skipped_no_shares == 0
            )
            if no_trading_day:
                status_str = "SUCCESS"
                step_status = StepStatus.SKIPPED
                notes = (
                    f"compute-market-cap on {exchange}: no bars for "
                    f"today ({_date.today()}) — market was closed "
                    f"(weekend or holiday). {cr.skipped_no_close} "
                    f"tickers skipped."
                )
            elif succeeded > 0:
                status_str = "SUCCESS"
                step_status = StepStatus.SUCCESS
                notes = (
                    f"compute-market-cap on {exchange} for today: "
                    f"{cr.succeeded_computed} COMPUTED, "
                    f"{cr.succeeded_approx} APPROX, "
                    f"{cr.skipped_no_close} no-close, "
                    f"{cr.skipped_no_shares} no-shares"
                )
            else:
                # No work succeeded but also nothing failed —
                # PARTIAL covers the unusual case where some
                # tickers lack shares data but the market was
                # open (otherwise no_trading_day would have
                # caught it).
                status_str = "PARTIAL"
                step_status = StepStatus.PARTIAL
                notes = (
                    f"compute-market-cap on {exchange}: no tickers "
                    f"had complete data ({cr.skipped_no_close} no-close, "
                    f"{cr.skipped_no_shares} no-shares)"
                )

            with transaction(db_engine) as conn:
                FundamentalsRunRepository(conn).close(
                    run_id, status=status_str,
                    succeeded=succeeded, failed=failed,
                    skipped=skipped,
                )
            return (step_status, succeeded, failed, skipped, notes)
        except Exception as e:
            with transaction(db_engine) as conn:
                FundamentalsRunRepository(conn).close(
                    run_id, status="FAILED",
                    succeeded=0, failed=0, skipped=0,
                    notes=str(e)[:500],
                )
            raise

    def _step_index_build():
        # Index build is per-exchange. It auto-discovers
        # qualifying industries (≥min_constituents EQ tickers)
        # and creates+rebalances an index for any that don't yet
        # exist. Industries that already have constituents are
        # left untouched and don't appear in any of the return
        # lists — only NEW work shows up.
        #
        # Return dict: {qualified, unqualified, indexed, failed}.
        #   - 'indexed' = newly created and rebalanced
        #   - 'failed' = qualified but rebalance couldn't pick
        #     enough constituents (e.g. industry sits right at
        #     the threshold)
        #   - 'qualified' / 'unqualified' = informational counts
        #     of the candidate universe
        report = run_industry_index_build(db_engine, exchange)
        indexed = report.get("indexed", [])
        failed_list = report.get("failed", [])
        qualified = report.get("qualified", [])
        unqualified = report.get("unqualified", [])
        n_indexed = len(indexed)
        n_failed = len(failed_list)

        # Status logic for the daily-run case:
        #   - If new indices were created and nothing failed: SUCCESS.
        #   - If new indices were created AND some failed: PARTIAL.
        #   - If NO new indices were created and nothing failed:
        #     SUCCESS (steady state — all qualified industries
        #     already indexed previously).
        #   - If NO new indices were created BUT some failed: PARTIAL
        #     (existing fleet is intact; this run couldn't add the
        #     marginal industries that sit just below the constituent
        #     threshold). NOT a fatal failure — downstream phases
        #     consume the existing fleet, not the marginal ones.
        if n_failed == 0:
            status = StepStatus.SUCCESS
        else:
            status = StepStatus.PARTIAL

        notes_parts = [f"build-industry-indices on {exchange}:"]
        notes_parts.append(
            f"qualified={len(qualified)}, unqualified={len(unqualified)}"
        )
        notes_parts.append(
            f"newly_indexed={n_indexed}, failed_to_pick={n_failed}"
        )
        if n_failed > 0 and n_indexed == 0:
            notes_parts.append(
                "(steady state — existing indices unchanged; "
                "failed-to-pick rows are sub-threshold industries)"
            )
        return (status, n_indexed, n_failed, 0, " ".join(notes_parts))

    def _step_index_values():
        # Index value backfill is per-industry (single-call). Loop
        # over all active industries, accumulate per-industry
        # outcomes into bulk counts.
        from datetime import date as _date
        from ..persistence.fundamentals_repo import (
            FundamentalsRunRepository,
            IndustryIndexRepository,
        )
        today = _date.today()
        with db_engine.connect() as conn:
            indices = IndustryIndexRepository(conn).list_active()
        succeeded = 0
        partial = 0
        failed = 0
        for idx in indices:
            try:
                run_id = run_index_value_backfill(
                    db_engine, idx.industry_name,
                    from_date=idx.base_date,
                    to_date=today,
                    force=force,
                )
                with db_engine.connect() as conn:
                    r = FundamentalsRunRepository(conn).get(run_id)
                if r is not None:
                    if r.status == "SUCCESS":
                        succeeded += 1
                    elif r.status == "PARTIAL":
                        partial += 1
                    else:
                        failed += 1
            except Exception:
                logger.exception(
                    "EoD pipeline: index-values failed for %s",
                    idx.industry_name,
                )
                failed += 1
        status = _status_from_counts(succeeded + partial, failed, 0)
        # PARTIAL outcomes count as "succeeded but with caveat" for
        # the pipeline's purposes.
        return (status, succeeded + partial, failed, 0,
                f"compute-index-values: {succeeded} SUCCESS, "
                f"{partial} PARTIAL, {failed} FAILED "
                f"(of {len(indices)} indices)")

    def _step_indicator_state_industries():
        report = run_indicator_state_for_all_industries(
            db_engine, force=force,
        )
        succeeded = len(report["succeeded"])
        failed = len(report["failed"])
        skipped = len(report["skipped"])
        status = _status_from_counts(succeeded, failed, skipped)
        return (status, succeeded, failed, skipped,
                f"compute-indicator-state for {succeeded} industries "
                f"(skipped {skipped}, failed {failed})")

    def _step_indicator_state_tickers():
        report = run_indicator_state_for_all_tickers(
            db_engine, timeframe, exchange=exchange, force=force,
        )
        succeeded = len(report["succeeded"])
        failed = len(report["failed"])
        skipped = len(report["skipped"])
        status = _status_from_counts(succeeded, failed, skipped)
        return (status, succeeded, failed, skipped,
                f"compute-indicator-state for {succeeded} tickers on "
                f"{exchange} tf={timeframe} (skipped {skipped}, "
                f"failed {failed})")

    def _step_signals_tickers():
        report = run_signal_events_for_all_tickers(
            db_engine, timeframe, exchange=exchange, force=force,
        )
        succeeded = len(report["succeeded"])
        failed = len(report["failed"])
        skipped = len(report["skipped"])
        status = _status_from_counts(succeeded, failed, skipped)
        return (status, succeeded, failed, skipped,
                f"compute-signals for {succeeded} tickers on "
                f"{exchange} tf={timeframe} (skipped {skipped}, "
                f"failed {failed})")

    # Assemble the step list in execution order, matching the
    # operator's spec:
    #   fetch stock data
    #     → calc per-stock EMAs
    #     → calc per-stock signals
    #     → build industry indices
    #     → calc industry index values
    #     → calc industry EMAs
    #     → calc industry signals (regions)
    # Per-stock comes before per-industry: they're independent (industry
    # indices are built from market cap aggregation, not ticker EMAs)
    # but the operator wants per-stock signals visible first.
    steps_to_run: list[tuple[str, Callable]] = [
        ("refresh-industry", _step_industry_refresh),
        ("refresh-shares", _step_shares_refresh),
        ("refresh-financials", _step_financials_refresh),
        ("compute-market-cap", _step_market_cap),
    ]
    if include_tickers:
        steps_to_run.extend([
            ("compute-indicator-state (tickers)",
             _step_indicator_state_tickers),
            ("compute-signals (tickers)", _step_signals_tickers),
        ])
    steps_to_run.extend([
        ("build-industry-indices", _step_index_build),
        ("compute-index-values", _step_index_values),
        ("compute-indicator-state (industries)",
         _step_indicator_state_industries),
    ])

    # Pre-populate the result with PENDING entries so callers can see
    # the full plan even if the pipeline aborts early.
    for name, _ in steps_to_run:
        result.steps.append(StepResult(name=name))

    # Execute steps. If any step fails fatally, mark the rest NOT_RUN.
    aborted = False
    for idx, (name, step_fn) in enumerate(steps_to_run):
        step_result = result.steps[idx]
        if aborted:
            step_result.status = StepStatus.NOT_RUN
            if progress_callback:
                progress_callback(step_result)
            continue
        step_result.status = StepStatus.RUNNING
        step_result.started_at = datetime.now(timezone.utc)
        logger.info("EoD pipeline: running step %r", name)
        try:
            status, succeeded, failed, skipped, notes = step_fn()
            step_result.status = status
            step_result.succeeded = succeeded
            step_result.failed = failed
            step_result.skipped = skipped
            step_result.notes = notes
            if status == StepStatus.FAILED:
                logger.error(
                    "EoD pipeline: step %r failed fatally, "
                    "aborting remaining steps", name,
                )
                aborted = True
        except Exception as e:
            logger.exception(
                "EoD pipeline: step %r raised, aborting", name,
            )
            step_result.status = StepStatus.FAILED
            step_result.error = str(e)
            aborted = True
        finally:
            step_result.finished_at = datetime.now(timezone.utc)
            if progress_callback:
                progress_callback(step_result)

    result.finished_at = datetime.now(timezone.utc)
    logger.info(
        "EoD pipeline finished: exchange=%s status=%s duration=%.1fs",
        exchange, result.overall_status,
        result.duration_seconds or 0.0,
    )

    # Phase 6-3: benchmark stats (alpha/beta/RS/R²). Cheap if
    # already current for the as_of_date — the skip guard returns
    # in milliseconds. Full recompute ~10-15 min.
    from datetime import date as _date
    from datetime import timedelta as _td
    today = _date.today()
    # Anchor as_of_date to the Monday of the current week (matches
    # bar_timestamp aggregation everywhere else in the system).
    as_of = today - _td(days=today.weekday())
    if _benchmark_stats_already_current(db_engine, as_of):
        logger.info(
            "benchmark stats already current for as_of=%s; skipping",
            as_of,
        )
    else:
        logger.info("benchmark stats: starting compute for as_of=%s", as_of)
        run_benchmark_stats_for_all_industries(db_engine, as_of_date=as_of)
        run_benchmark_stats_for_all_tickers(db_engine, as_of_date=as_of)
        logger.info("benchmark stats: complete")
    return result


def _status_from_run(run) -> StepStatus:
    """Map a FundamentalsRun row's status string to StepStatus."""
    if run is None:
        return StepStatus.FAILED
    return {
        "SUCCESS": StepStatus.SUCCESS,
        "PARTIAL": StepStatus.PARTIAL,
        "FAILED": StepStatus.FAILED,
        "RUNNING": StepStatus.RUNNING,  # shouldn't happen post-close
    }.get(run.status, StepStatus.FAILED)


def _status_from_counts(succeeded: int, failed: int, skipped: int) -> StepStatus:
    """Derive a StepStatus from succeeded/failed/skipped counts. Used
    by bulk-runner reports where there's no FundamentalsRun row at
    the bulk level (each industry/ticker has its own run row)."""
    if failed > 0 and succeeded == 0:
        return StepStatus.FAILED
    if failed > 0:
        return StepStatus.PARTIAL
    return StepStatus.SUCCESS

# ---------- Phase 6-3: benchmark stats hook ---------- #


def _benchmark_stats_already_current(db_engine, as_of_date) -> bool:
    """Skip-if-recent guard for the benchmark stats step.

    Returns True if stock_benchmark_stats already has rows for the
    given as_of_date — meaning a prior EoD run on the same date
    already computed them. Makes mid-week EoD runs (which fire daily
    but don't change the weekly-bar close) sub-second no-ops on this
    step, while Friday EoD runs (when the weekly bar actually closes)
    do the full ~10-15 min compute.
    """
    from sqlalchemy import text as _text
    with db_engine.connect() as conn:
        row = conn.execute(_text("""
            SELECT 1 FROM stock_benchmark_stats
            WHERE as_of_date = :d LIMIT 1
        """), {"d": as_of_date}).first()
    return row is not None

