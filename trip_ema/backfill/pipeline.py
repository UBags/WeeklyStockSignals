"""V1 backfill pipeline orchestration — Step 11b1.

10-stage pipeline that takes a date range + ticker scope and brings
the entire V1 data model into a consistent state for that range:

  Stage 1: (if refetch_bars) yfinance fetch
  Stage 2: stock_indicator_state recompute (full mode, per ticker)
  Stage 3: stock_indicator_enrichment recompute (full mode, per ticker)
  Stage 4: industry_index_value recompute (all industries, full range)
  Stage 5: industry_indicator_state recompute (all industries, full)
  Stage 6: industry_indicator_enrichment recompute (all industries, full)
  Stage 7: DELETE historical_signals for scoped tickers, >= from_monday
  Stage 8: Walk Mondays, derive at each, INSERT historical_signals
  Stage 9: Rebuild current_signals for scoped tickers
           (latest-bar flip semantics)
  Stage 10: cache reload — service-side; CLI doesn't touch

Industry stages (4, 5, 6) always run for ALL industries — keeps the
pipeline simple. Cost is ~30s extra vs the (~24 industries) subset
that's technically impacted, which isn't worth the targeting logic.

Date semantics: `date_from` rounds DOWN to its containing Monday
(weekly bars are IST-Monday-anchored). `date_to` defaults to today;
the Monday walk goes up through `monday_of(date_to)` inclusive — the
in-progress week IS included, which produces preliminary signals
that the next live cycle will overwrite. This is acceptable per
spec: live ticks normally rewrite the latest 2 weeks of state.

Marker lifecycle:
  - `BackfillRunsRepository.start()` at entry (raises IntegrityError
    if another backfill is already active; caller catches and
    reports "use --release-lock if stale")
  - `BackfillRunsRepository.finish()` in a `finally` clause so the
    live cycle's deferral check (Step 11a) always re-engages, even
    on stage failure.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from collections.abc import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .bulk_daywalk import backfill_daywalk_bulk

from ..domain import Timeframe
from ..domain.market_session import (
    day_bounds,
    monday_of,
    week_anchor,
)
from ..etl.runner import fetch_bars_for_tickers, refresh_listings, run_backfill
from ..fundamentals.merged_state_enrichment import (  # MERGED-STATE-ENRICHMENT
    populate_stock_state_and_enrichment_for_ticker,
)
from ..fundamentals.v1_runners import (
    populate_industry_index_value_for_all,
)
from ..orchestrator.industry_stages import (
    _stage_industry_enrichment,
    _stage_industry_state,
)
from ..persistence import transaction
from .phase_ledger import run_phase, reset_phases
from ..fundamentals.parallel import (
    engine_spec_from_config,
    fan_out,
    fan_out_processes,
)
from ..fundamentals.market_cap_compute import populate_market_cap_range
from ..fundamentals.data_fidelity import audit_exchange
from ..domain.market_session import market_today, market_tz
from ..fundamentals.runners import (
    run_financials_refresh,
    run_industry_refresh,
    run_shares_refresh,
    build_all_industry_index_chains,
    clear_industry_derived_for_rebuild,
)
from ..fundamentals.signal_enrichment_runner import backfill_signal_enrichment
from ..fundamentals.sector_metrics_runner import backfill_sector_metrics
from ..fundamentals.industry_regime_runner import backfill_industry_regime_matrix
# BENCHMARK-STATS-STAGE: rank_score in the email comes from these tables.
from ..fundamentals.benchmark_stats_runners import (
    run_benchmark_stats_for_all_industries,
    run_benchmark_stats_for_all_tickers,
)
from ..fundamentals.signal_ensemble_runner import backfill_signal_ensemble_features
from ..fundamentals.signal_labels_runner import backfill_signal_labels
from ..persistence.backfill_runs_repo import BackfillRunsRepository
from ..persistence.historical_signals_repo import (
    HistoricalSignalsRepository,
)

logger = logging.getLogger(__name__)



# ============================================================
# Result dataclass
# ============================================================

@dataclass(frozen=True, slots=True)
class BackfillResult:
    """Per-run summary returned to the caller (CLI + tests).

    `failed_stage` is the name of the first stage that raised, or
    None if the whole pipeline succeeded. `error_message` carries
    the exception's str()."""
    run_id: int
    started_at: datetime
    finished_at: datetime
    date_from: date
    date_to: date
    from_monday: date
    to_monday: date
    exchange: str
    ticker_scope_size: int   # actual ticker_id count (after ALL→list resolve)
    ticker_scope_label: str   # 'ALL' or comma-separated symbols
    refetch_bars: bool
    bars_fetch_run_id: int | None     # etl_runs.run_id, or None if skipped
    stock_state_tickers_done: int
    # The post-flight verdict. `fidelity_ready` is False when the pipeline
    # finished but the data it built is still incomplete — which is NOT the same
    # as a stage failing, and used to be invisible. The CLI exits non-zero on it.
    fidelity_ready: bool | None = None
    fidelity_gaps: list[str] = field(default_factory=list)
    fidelity_report: str = ""
    stock_state_failed: list[tuple[int, str]] = field(
        default_factory=list,
    )
    stock_enrichment_tickers_done: int = 0
    stock_enrichment_failed: list[tuple[int, str]] = field(
        default_factory=list,
    )
    iiv_industries_done: int = 0
    industry_state_done: int = 0
    industry_enrichment_done: int = 0
    # BENCHMARK-STATS-STAGE (6d). `skipped` is EXPECTED, not an error: a ticker
    # whose industry has no industry_index_value rows (sub-threshold industry)
    # gets no INDUSTRY stats, and NIFTYBEES is skipped for the NIFTY leg
    # (self-vs-self). Kept separate from `failed` so a big skip count doesn't
    # read as a big failure count.
    benchmark_tickers_done: int = 0
    benchmark_tickers_skipped: int = 0
    benchmark_industries_done: int = 0
    benchmark_industries_skipped: int = 0
    benchmark_failed: list[tuple[str, str]] = field(
        default_factory=list,
    )
    historical_signals_deleted: int = 0
    historical_signals_inserted: int = 0
    current_signals_upserted: int = 0
    current_signals_deleted: int = 0
    days_walked: int = 0
    failed_stage: str | None = None
    error_message: str | None = None


# ============================================================
# Entry point
# ============================================================

class BlockedByFidelityError(RuntimeError):
    """A structural defect makes this backfill unsafe to run.

    Raised BEFORE any work, not after. See _stage_audit_preflight.
    """


class ForeignIndustryLayerError(RuntimeError):
    """The industry layer already belongs to a different exchange.

    Raised INSTEAD of silently corrupting it. See
    _assert_industry_layer_belongs_to.
    """


def _assert_industry_layer_belongs_to(db_engine: Engine, exchange: str) -> None:
    """Refuse to rebuild industry indices that belong to another exchange.

    Not one of the nine tables in the industry layer has an exchange column:

        industry_index                PK (industry_name)
        industry_index_value          PK (industry_name, trade_date)
        industry_indicator_state      PK (industry_name, timeframe, date)
        industry_index_constituent, industry_index_period,
        industry_indicator_enrichment, industry_benchmark_stats,
        industry_regime_breadth, sector_stock_leadlag

    So NSE's "Software - Infrastructure" and NYSE's "Software - Infrastructure"
    are THE SAME ROW. Running `backfill --exchange NYSE` against a database
    holding NSE's indices does not fail — it UPSERTS over them, and every NSE
    signal computed afterwards derives from an index built out of NYSE
    constituents. Silently. With a green run.

    That is the worst failure shape this codebase can produce, so it is refused.

    The exchange is derived through the constituents, since the tables themselves
    cannot say:

        industry_index_constituent.ticker_id -> tickers.exchange

    An empty industry layer belongs to nobody, and a fresh database can be
    pointed at any exchange.

    There is deliberately NO override. Until the schema can represent two
    exchanges, the operation this would enable is always wrong, and a --force
    flag would just be a footgun with a safety catch. When the exchange-key
    migration lands, this guard is REPLACED, not bypassed.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT t.exchange
                  FROM industry_index_constituent c
                  JOIN tickers t ON t.ticker_id = c.ticker_id
            """)
        )
        present = {r[0] for r in rows}

    foreign = present - {exchange}
    if foreign:
        raise ForeignIndustryLayerError(
            f"REFUSING to backfill exchange {exchange!r}: the industry layer "
            f"already holds indices built from {sorted(foreign)} constituents, "
            f"and NONE of the nine industry tables has an exchange column "
            f"(industry_index_value's PK is (industry_name, trade_date)). "
            f"Rebuilding would UPSERT over {sorted(foreign)}'s industry indices "
            f"and every signal derived from them would be silently wrong. "
            f"Use a separate database per exchange until the industry layer is "
            f"keyed by exchange."
        )


def backfill(
    db_engine: Engine,
    config,
    *,
    date_from: date,
    date_to: date | None = None,
    exchange: str = "NSE",
    stocks: list[int] | None = None,
    refetch_bars: bool = True,
    refresh_reference: bool = True,
    resume: bool = True,
    progress_cb: Callable[[str, str], None] | None = None,
) -> BackfillResult:
    """Operator entry point for the V1 backfill pipeline.

    Args:
        db_engine: SQLAlchemy Engine.
        config: trip_ema Config (used by stage 1's yfinance fetch).
        date_from: start of range (inclusive). Rounded DOWN to the
            containing Monday before walking.
        date_to: end of range (inclusive). Default: today.
        exchange: 'NSE' / 'BSE' / 'NYSE'.
        stocks: list of ticker_ids to scope to. None means all
            active tickers on `exchange`.
        refetch_bars: when False, skip stage 1 (yfinance fetch).
        progress_cb: optional callback(stage_name, message) called
            at the start of each stage and at major milestones.
            Useful for CLI progress display.

    Returns:
        BackfillResult with per-stage counters + failure indicator.

    Raises:
        IntegrityError: another backfill is already active (the
            unique partial index on `backfill_runs` fired). Caller
            should suggest `--release-lock` or wait for the prior
            run to finish. The pipeline itself does NOT release the
            lock; that's an explicit operator action.
        ValueError: invalid args (date_from > date_to, empty stocks,
            etc).
    """
    if date_to is None:
        date_to = market_today(exchange)
    if date_from > date_to:
        raise ValueError(
            f"date_from ({date_from}) must be <= date_to ({date_to})"
        )

    started_at = datetime.now(UTC)
    from_monday = _monday_of(date_from)
    to_monday = _monday_of(date_to)
    ticker_scope_label = (
        "ALL" if stocks is None
        else ",".join(str(t) for t in sorted(stocks))
    )

    # ---- Resolve ticker scope upfront so all stages share it ----
    # BEFORE anything is written, and before a run row is even opened: the
    # industry layer is shared across exchanges by omission, not by design. If it
    # belongs to someone else, stop here rather than discovering it in the
    # signals a week later.
    _assert_industry_layer_belongs_to(db_engine, exchange)

    # ---- Stage 0a: fidelity pre-flight ----
    # Ask, BEFORE spending six hours, whether this run can possibly produce
    # correct data.
    #
    # BLOCKED is a structural defect that no amount of correct data fixes — today
    # that is industry_index_value's PK (industry_name, trade_date), which has no
    # exchange column. Discovering that AFTER the run is discovering it too late:
    # the corruption is already written.
    #
    # Gaps are NOT fatal here. They are the REASON the backfill is running. But
    # they are logged, because a run starting from 198 truncated tickers should
    # say so — and because the post-flight verdict is meaningless without a
    # before.
    _emit_progress(progress_cb, "stage_0a_audit_preflight", "starting")
    _pre = audit_exchange(
        db_engine, exchange, gap_days=config.etl.max_coverage_gap_days,
    )
    for _line in _pre.render().splitlines():
        logger.info("preflight| %s", _line)

    if _pre.blockers:
        _names = ", ".join(d.dataset for d in _pre.blockers)
        raise BlockedByFidelityError(
            f"REFUSING to backfill {exchange}: {len(_pre.blockers)} structural "
            f"blocker(s) — {_names}. These are defects no amount of correct data "
            f"repairs, so running the pipeline would write corrupt data rather "
            f"than fix it. Run `audit --exchange {exchange}` for the detail."
        )

    logger.info(
        "preflight: %d gap(s) to fill, 0 blockers — proceeding",
        len(_pre.gaps),
    )
    _emit_progress(
        progress_cb, "stage_0a_audit_preflight",
        f"completed: {len(_pre.gaps)} gap(s) to fill",
    )

    # ---- Stage 0: listings ----
    # This MUST run before _resolve_tickers, which raises "no active tickers
    # resolved" on an exchange that has none. On a fresh database that is exactly
    # the situation, so the sync that would FIX it would never get to run — the
    # backfill would refuse to build the very universe it needs.
    #
    # Outside the phase ledger by necessity (no run_id yet) and by nature: it is
    # an idempotent upsert of the exchange's listings and takes about a minute.
    #
    # Skipped for a partial run: adding tickers to the universe midway through a
    # scoped run changes the scope under the run's feet.
    #
    # NOTE: refresh_listings raises UnsupportedExchangeError for anything but NSE
    # — there is no listings source for any other exchange yet. A NYSE backfill
    # therefore fails HERE, with that explicit message, rather than dying later at
    # _resolve_tickers with a mystifying "no active tickers".
    if refresh_reference and stocks is None:
        _emit_progress(progress_cb, "stage_0_listings", "starting")
        _lr = refresh_listings(db_engine, config, exchange=exchange)
        logger.info(
            "backfill: listings synced — new=%d renamed=%d delisted=%d",
            _lr.sync_result.new_listings,
            _lr.sync_result.renamed_symbols,
            _lr.sync_result.delisted,
        )
        _emit_progress(
            progress_cb, "stage_0_listings",
            f"completed: {_lr.sync_result.new_listings} new",
        )
    elif not refresh_reference:
        _emit_progress(
            progress_cb, "stage_0_listings", "skipped (--no_refresh_reference)",
        )
    else:
        _emit_progress(
            progress_cb, "stage_0_listings", "skipped (partial ticker scope)",
        )

    scoped_tickers = _resolve_tickers(db_engine, exchange, stocks)
    if not scoped_tickers:
        raise ValueError(
            f"no active tickers resolved for exchange={exchange} "
            f"with stocks={stocks!r}",
        )
    logger.info(
        "backfill: scope=%d tickers, range=%s..%s (mondays %s..%s) "
        "exchange=%s refetch_bars=%s",
        len(scoped_tickers), date_from, date_to,
        from_monday, to_monday, exchange, refetch_bars,
    )

    # ---- start() the marker. If unique-active constraint fires, the
    # IntegrityError propagates and the caller (CLI) handles it. ----
    repo = BackfillRunsRepository
    with transaction(db_engine) as conn:
        run_id = repo(conn).start(
            pid=os.getpid(),
            exchange=exchange,
            date_from=date_from,
            date_to=date_to,
            refetch_bars=refetch_bars,
            ticker_scope=ticker_scope_label,
        )
    logger.info("backfill: run_id=%d started", run_id)

    # resume=False forces a full rebuild: clear the phase ledger so every
    # phase re-runs. resume=True (default) leaves the ledger, so already-
    # COMPLETE phases are skipped and the run picks up where it stopped.
    if not resume:
        n_cleared = reset_phases(db_engine, exchange)
        logger.info(
            "backfill: resume=False — cleared %d phase-ledger rows for %s",
            n_cleared, exchange,
        )
    _force = not resume

    # ---- Scoped runs do NOT participate in the phase ledger ----
    #
    # pipeline_phase_status's PK is (exchange, phase_name). It records that
    # "fetch_bars is COMPLETE for NSE" — but NOT for which tickers. A backfill's
    # work is scope-dependent: --stocks DABUR,MARICO,CONCOR fetches three, a full
    # run fetches 2,673, and both would write the same row.
    #
    # With resume=True that means a scoped run skips every phase the last full run
    # completed — observed: a 3-ticker diagnostic skipped the entire pipeline,
    # walked 0 days and never contacted Yahoo. Wasteful, but visible.
    #
    # With --rebuild it is far worse: the scoped run clears the ledger, fetches
    # THREE tickers, and marks fetch_bars COMPLETE. The next FULL backfill reads
    # that row and skips fetching altogether — 2,670 tickers never fetched, green
    # run, no error. A completion marker that does not know what it completed.
    #
    # So a scoped run neither reads nor writes the ledger: every phase always
    # executes (no false skips) and none is ever recorded (no false completions).
    # The ledger keeps meaning exactly what it says — "this phase finished for the
    # WHOLE EXCHANGE" — because only full-exchange runs write to it.
    #
    # NB: assigning `run_phase` here makes it a LOCAL for the whole of backfill(),
    # so all 17 call sites below are covered by this one binding. That is
    # deliberate: editing 17 sites is 17 chances to miss one, and missing one
    # means a scoped run still writes a false COMPLETE for that phase — which is
    # the entire bug.
    def _scoped_run_phase(_db, _ex, phase_name, fn, **_kwargs):
        """Run the phase; tell the ledger nothing."""
        logger.info(
            "phase %s/%s: scoped run (%d ticker(s)) — executing, ledger bypassed",
            _ex, phase_name, len(stocks or []),
        )
        return fn()

    if stocks is not None:
        run_phase = _scoped_run_phase

    # ---- The pipeline body — wrap in try/finally so finish() ALWAYS
    # gets called, freeing the live cycle's deferral check. ----
    result_kwargs: dict = {
        "run_id": run_id,
        "started_at": started_at,
        "date_from": date_from,
        "date_to": date_to,
        "from_monday": from_monday,
        "to_monday": to_monday,
        "exchange": exchange,
        "ticker_scope_size": len(scoped_tickers),
        "ticker_scope_label": ticker_scope_label,
        "refetch_bars": refetch_bars,
        "bars_fetch_run_id": None,
        "stock_state_tickers_done": 0,
    }
    failed_stage: str | None = None
    error_message: str | None = None

    try:
        # ---- Stages 0b-0d: reference data (industry, shares, financials) ----
        #
        # These runners have always existed; backfill() simply never called them,
        # so it began at OHLCV and assumed a reference layer nothing built.
        #
        # only_missing=True is what makes them affordable enough to belong here.
        # A full sweep is ~90 minutes PER RUNNER at 2s/ticker; a gap-only sweep
        # over a satisfied layer costs seconds, because tickers with data are
        # skipped and tickers the provider has nothing for are recorded in
        # fundamentals_fetch_status and skipped too.
        #
        # Ordering: shares MUST precede stage 3b (market_cap is derived from
        # instrument_shares x ohlcv_bars) and industry MUST precede the index
        # build. Both hold.
        if refresh_reference:
            for _label, _phase, _fn in (
                ("stage_0b_industry", "reference_industry",
                 run_industry_refresh),
                ("stage_0c_shares", "reference_shares", run_shares_refresh),
                ("stage_0d_financials", "reference_financials",
                 run_financials_refresh),
            ):
                _emit_progress(progress_cb, _label, "starting (gaps only)")
                run_phase(
                    db_engine, exchange, _phase,
                    # force=True: the runners carry an 80-day
                    # skip-if-the-RUN-was-recent guard, which exists to stop a
                    # needless FULL re-sweep. A gap-only pass is precisely the
                    # thing that guard should not block — and the phase ledger
                    # already handles "don't redo completed work" for us.
                    lambda fn=_fn: fn(
                        db_engine, exchange,
                        force=True, only_missing=True,
                    ),
                    backfill_run_id=run_id, force=_force,
                )
                _emit_progress(progress_cb, _label, "completed")
        else:
            _emit_progress(
                progress_cb, "stage_0b_reference",
                "skipped (--no_refresh_reference)",
            )

        # ---- Stage 1: yfinance bar refetch ----
        if refetch_bars:
            _emit_progress(progress_cb, "stage_1_fetch_bars", "starting")
            _r = run_phase(
                db_engine, exchange, "fetch_bars",
                lambda: _stage_fetch_bars(
                    db_engine, config,
                    exchange=exchange,
                    scoped_tickers=scoped_tickers,
                    stocks=stocks,
                    start_date=date_from,
                    end_date=date_to,
                ),
                backfill_run_id=run_id, force=_force,
            )
            if _r is not None:
                result_kwargs["bars_fetch_run_id"] = _r
        else:
            _emit_progress(
                progress_cb,
                "stage_1_fetch_bars", "skipped (--no_fetch_bars)",
            )

        # ---- Stage 2: stock indicator state ----
        _emit_progress(
            progress_cb, "stage_2_stock_state",
            f"starting ({len(scoped_tickers)} stocks)",
        )
        _r = run_phase(
            db_engine, exchange, "stock_state",
            lambda: _stage_stock_state_full(
                db_engine, config, scoped_tickers, progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        if _r is not None:
            done, failed = _r
            _emit_progress(
                progress_cb, "stage_2_stock_state",
                f"completed: {done}/{len(scoped_tickers)} stocks"
                + (f" ({len(failed)} failed)" if failed else ""),
            )
            result_kwargs["stock_state_tickers_done"] = done
            result_kwargs["stock_state_failed"] = failed
        else:
            _emit_progress(
                progress_cb, "stage_2_stock_state",
                "skipped (already complete)",
            )

        # ---- Stage 3: stock indicator enrichment ----
        _emit_progress(
            progress_cb, "stage_3_stock_enrichment",
            f"starting ({len(scoped_tickers)} stocks)",
        )
        _r = run_phase(
            db_engine, exchange, "stock_enrichment",
            lambda: _stage_stock_enrichment_full(
                db_engine, scoped_tickers, progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        if _r is not None:
            done, failed = _r
            _emit_progress(
                progress_cb, "stage_3_stock_enrichment",
                f"completed: {done}/{len(scoped_tickers)} stocks"
                + (f" ({len(failed)} failed)" if failed else ""),
            )
            result_kwargs["stock_enrichment_tickers_done"] = done
            result_kwargs["stock_enrichment_failed"] = failed
        else:
            _emit_progress(
                progress_cb, "stage_3_stock_enrichment",
                "skipped (already complete)",
            )

        # ---- Stage 3b: market cap (per exchange) ----
        # These three build the per-exchange industry indices that
        # stages 4-6 operate on. Without them, iiv/state/enrichment
        # process 0 industries.
        _emit_progress(progress_cb, "stage_3b_market_cap", "starting")
        _r = run_phase(
            db_engine, exchange, "market_cap",
            lambda: _stage_market_cap(
                db_engine, exchange=exchange,
                date_from=date_from, date_to=date_to,
                progress_cb=progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        if _r is not None:
            _emit_progress(
                progress_cb, "stage_3b_market_cap",
                f"completed: ~{_r} mcap rows",
            )

        # ---- SCOPED-INDUSTRY-REBUILD: chain build vs scoped reuse. ----
        # A forced run is FULL (rebuild chains from origin) when date_from
        # reaches the index origin, and SCOPED (reuse existing constituents)
        # when date_from is after it. Chain-building is inherently a
        # from-origin operation (build_all_industry_index_chains anchors on
        # the earliest mcap date), so a later date_from can only reuse chains,
        # never rebuild them for a window. iiv[D] is date-independent (a
        # weighted sum of constituent closes on D), so windowing iiv is
        # correct; the full state+enrichment recompute in stages 5/6 then
        # propagates the windowed iiv change forward through the stateful
        # EMA/ATR indicators. origin = MIN(base_date) over ACTIVE indices;
        # chains are "built" iff industry_index_period has rows for an
        # ACTIVE index (the global clear wipes periods+constituents but keeps
        # the industry_index definitions, so base_date alone is insufficient).
        _scoped_industry_rebuild = False
        if _force:
            with db_engine.connect() as _c:
                _origin = _c.execute(text(
                    "SELECT MIN(base_date) FROM industry_index "
                    "WHERE status = 'ACTIVE'"
                )).scalar()
                _chains_built = bool(_c.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM industry_index_period p "
                    "JOIN industry_index i "
                    "ON i.industry_name = p.industry_name "
                    "WHERE i.status = 'ACTIVE')"
                )).scalar())
            _scoped_industry_rebuild = (
                _origin is not None and date_from > _origin
            )
            if _scoped_industry_rebuild and not _chains_built:
                raise ValueError(
                    f"scoped rebuild: date_from ({date_from}) is after the "
                    f"industry index origin ({_origin}), which reuses existing "
                    f"constituents — but no industry chains are built. Run a "
                    f"full rebuild first with --date_from <= {_origin}."
                )

        if _scoped_industry_rebuild:
            # SCOPED: reuse constituents. Skip the global (unscoped) clear —
            # it deletes constituents+periods for ALL active industries and
            # would force a from-scratch rebuild — and skip stage_3c chain
            # build. Derived data for [date_from, today] is recomputed by
            # stages 3d/5/6 below.
            _emit_progress(
                progress_cb, "stage_3c_industry_index_build",
                "skipped (scoped rebuild — reusing existing constituents)",
            )
        else:
            # FULL: clear DERIVED industry data so the rebuild is from-scratch.
            # The index pipeline writes insert-if-missing (ON CONFLICT DO
            # NOTHING), so a forced re-run would else keep stale values/state
            # and invert rebalance periods.
            if _force:
                _emit_progress(
                    progress_cb, "industry_rebuild_clear",
                    "clearing derived industry tables for --rebuild",
                )
                clear_industry_derived_for_rebuild(db_engine)

            # ---- Stage 3c: build industry indices (per exchange) ----
            _emit_progress(
                progress_cb, "stage_3c_industry_index_build", "starting",
            )
            _r = run_phase(
                db_engine, exchange, "industry_index_build",
                lambda: _stage_industry_index_build(
                    db_engine, exchange=exchange, through=date_to,
                    progress_cb=progress_cb,
                ),
                backfill_run_id=run_id, force=_force,
            )
            if _r is not None:
                _emit_progress(
                    progress_cb, "stage_3c_industry_index_build",
                    f"completed: {_r} chains",
                )

        # ---- Stage 3d: backfill industry index values ----
        # The ONLY industry-index-value pass. Shares the
        # populate_industry_index_value_for_all populator with the live
        # intraday/eod cycle; the old duplicate "stage 4 iiv" pass over
        # the same table and range has been removed.
        _emit_progress(
            progress_cb, "stage_3d_industry_index_values", "starting",
        )
        _r = run_phase(
            db_engine, exchange, "industry_index_values",
            lambda: _stage_industry_index_values(
                db_engine, date_from=date_from, date_to=date_to,
                progress_cb=progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        if _r is not None:
            result_kwargs["iiv_industries_done"] = _r
            _emit_progress(
                progress_cb, "stage_3d_industry_index_values",
                f"completed: {_r} indices",
            )

        # ---- Stage 5: industry indicator state ----
        _emit_progress(progress_cb, "stage_5_industry_state", "starting")
        _r = run_phase(
            db_engine, exchange, "industry_state",
            lambda: _stage_industry_state(
                db_engine, mode="full", progress_cb=progress_cb,
            )[0],
            backfill_run_id=run_id, force=_force,
        )
        if _r is not None:
            result_kwargs["industry_state_done"] = _r
            _emit_progress(
                progress_cb, "stage_5_industry_state",
                f"completed: {_r} indicators",
            )

        # ---- Stage 6: industry indicator enrichment ----
        _emit_progress(
            progress_cb, "stage_6_industry_enrichment", "starting",
        )
        _r = run_phase(
            db_engine, exchange, "industry_enrichment",
            lambda: _stage_industry_enrichment(
                db_engine, mode="full", progress_cb=progress_cb,
            )[0],
            backfill_run_id=run_id, force=_force,
        )
        if _r is not None:
            result_kwargs["industry_enrichment_done"] = _r
            _emit_progress(
                progress_cb, "stage_6_industry_enrichment",
                f"completed: {_r} indicators",
            )

        # ---- Stage 6b: sector cross-section (rs_rank) ----
        # Per-industry weekly rs_rank into sector_stock_leadlag; joined at
        # read-time, independent of the per-signal enrichment vector.
        _emit_progress(
            progress_cb, "stage_6b_sector_metrics", "starting",
        )
        _sm = run_phase(
            db_engine, exchange, "sector_metrics",
            lambda: backfill_sector_metrics(
                db_engine, exchange, progress_cb=progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        _emit_progress(
            progress_cb, "stage_6b_sector_metrics",
            "done" if _sm is not None else "skipped (resume)",
        )

        # ---- Stage 6c: industry x industry regime matrix (§17) ----
        _emit_progress(
            progress_cb, "stage_6c_regime_matrix", "starting",
        )
        _rm = run_phase(
            db_engine, exchange, "regime_matrix",
            lambda: backfill_industry_regime_matrix(
                db_engine, exchange, progress_cb=progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        _emit_progress(
            progress_cb, "stage_6c_regime_matrix",
            "done" if _rm is not None else "skipped (resume)",
        )

        # ---- Stage 6d: benchmark stats (BENCHMARK-STATS-STAGE) ----
        # Populates stock_benchmark_stats + industry_benchmark_stats. These were
        # previously reachable ONLY via a separate manual CLI command, so both
        # tables sat empty — which meant fetch_rank_scores returned nothing,
        # every emailed signal scored rank_score = 0.0, and the email's
        # `ORDER BY rank_score DESC, ticker_id ASC` silently degraded to
        # ticker_id order. Nothing surfaced the omission.
        #
        # Must run AFTER industry_index_values (3d) — the INDUSTRY benchmark leg
        # reads industry_index_value — and BEFORE notify, which is the point:
        # notify -> compose_email -> fetch_rank_scores.
        #
        # Nothing on the signal path depends on it: signal_enrichment reads
        # industry_index_value directly, not these tables. So this stage cannot
        # perturb signal correctness.
        _emit_progress(
            progress_cb, "stage_6d_benchmark_stats", "starting",
        )
        _bm = run_phase(
            db_engine, exchange, "benchmark_stats",
            lambda: _stage_benchmark_stats(
                db_engine, exchange=exchange, progress_cb=progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        if _bm is not None:
            # NOTE: the pipeline has NO mutable `result` object during the
            # stages — it accumulates into result_kwargs and constructs
            # BackfillResult at the very end. Writing `result.x = ...` here
            # would compile cleanly and then NameError at runtime, hours into a
            # backfill.
            (result_kwargs["benchmark_tickers_done"],
             result_kwargs["benchmark_tickers_skipped"],
             result_kwargs["benchmark_industries_done"],
             result_kwargs["benchmark_industries_skipped"],
             result_kwargs["benchmark_failed"]) = _bm
        _emit_progress(
            progress_cb, "stage_6d_benchmark_stats",
            "done" if _bm is not None else "skipped (resume)",
        )

        # ---- Stage 7: DELETE historical_signals in range ----
        _emit_progress(
            progress_cb, "stage_7_delete_historical", "starting",
        )
        _r = run_phase(
            db_engine, exchange, "delete_historical",
            lambda: _stage_delete_historical(
                db_engine,
                scoped_tickers=scoped_tickers,
                from_monday=from_monday,
                to_date=date_to,
                exchange=exchange,
            ),
            backfill_run_id=run_id, force=_force,
        )
        if _r is not None:
            result_kwargs["historical_signals_deleted"] = _r

        # ---- Stages 8+9: per-day walk on the unified wrapper ----
        # Each trading day D in the range runs process_signals_for_day in
        # backfill mode: per-day historical (delete D + insert) and current
        # only when D == today (the final day). Replaces the legacy Monday-
        # walk + separate current rebuild.
        _emit_progress(
            progress_cb, "stage_8_walk_days", "starting",
        )
        # The market's today, not the machine's. On a NUC set to Asia/Kolkata
        # running NSE these are the same date, which is precisely why a hardcoded
        # IST survived here unnoticed.
        _today = datetime.now(market_tz(exchange)).date()
        _r = run_phase(
            db_engine, exchange, "daywalk",
            # BULK-DAYWALK: the per-day walk ran process_signals_for_day once
            # per trading day, fanning out over every ticker, each doing two
            # point queries -> ~13.3M derive calls on a full NSE rebuild.
            # Walked days ARE the distinct stock_indicator_state dates, and
            # derive compares state[D] vs state[D-1], so the whole walk is
            # equivalent to "emit on every consecutive state-row pair whose
            # regimes differ" — ~2 queries per ticker. Same _classify_regime_
            # change, same price lookup, same computed_at. Parity-verified
            # against the per-day path (0 missing / 0 extra / 0 mismatch).
            lambda: backfill_daywalk_bulk(
                db_engine,
                scoped_tickers=scoped_tickers,
                date_from=date_from,
                date_to=date_to,
                today=_today,
                exchange=exchange,
                progress_cb=progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        if _r is not None:
            (days_walked, inserted, upserted, deleted,
             today_walked) = _r
            result_kwargs["days_walked"] = days_walked
            result_kwargs["historical_signals_inserted"] = inserted
            result_kwargs["current_signals_upserted"] = upserted
            result_kwargs["current_signals_deleted"] = deleted
        else:
            # daywalk already COMPLETE (resume) — don't notify off a walk
            # we didn't run; today_walked stays False.
            today_walked = False

        # ---- Stage 8b: per-signal enrichment ----
        # Reads the historical_signals the daywalk just wrote and
        # materialises the causal per-signal feature vector.
        _emit_progress(
            progress_cb, "stage_8b_signal_enrichment", "starting",
        )
        _enr = run_phase(
            db_engine, exchange, "signal_enrichment",
            lambda: backfill_signal_enrichment(
                db_engine, exchange,
                scoped_tickers=scoped_tickers,
                progress_cb=progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        _emit_progress(
            progress_cb, "stage_8b_signal_enrichment",
            "done" if _enr is not None else "skipped (resume)",
        )

        # ---- Stage 8c: per-signal labels (retrospective target y) ----
        # Computes forward-return labels for the signal_enrichment rows
        # into signal_labels; re-runs finalise a label (horizon_complete)
        # once all 13 forward weeks exist.
        _emit_progress(
            progress_cb, "stage_8c_signal_labels", "starting",
        )
        _lbl = run_phase(
            db_engine, exchange, "signal_labels",
            lambda: backfill_signal_labels(
                db_engine, exchange,
                scoped_tickers=scoped_tickers,
                progress_cb=progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        _emit_progress(
            progress_cb, "stage_8c_signal_labels",
            "done" if _lbl is not None else "skipped (resume)",
        )

        # ---- Stage 8d: per-signal ensemble features (§17.5) ----
        _emit_progress(
            progress_cb, "stage_8d_signal_ensemble", "starting",
        )
        _sef = run_phase(
            db_engine, exchange, "signal_ensemble_features",
            lambda: backfill_signal_ensemble_features(
                db_engine, exchange, progress_cb=progress_cb,
            ),
            backfill_run_id=run_id, force=_force,
        )
        _emit_progress(
            progress_cb, "stage_8d_signal_ensemble",
            "done" if _sef is not None else "skipped (resume)",
        )

        # ---- Notify subscribers — IFF this backfill is current
        # through today (to_monday == the current IST week's Monday).
        # A historical-range backfill must NOT email anyone. When it
        # does fire, it calls the exact same notify_subscribers the
        # live cycle uses; subscriber cadence
        # (find_due_for_notification) dedups against any live cycle
        # that already notified today. ----
        if today_walked:
            from ..config.brevo_secrets import load_brevo_api_key
            from ..orchestrator.notify_subscribers import (
                notify_subscribers,
            )
            brevo_config = getattr(
                getattr(config, "transports", None), "brevo", None,
            )
            if brevo_config is None:
                _emit_progress(
                    progress_cb, "notify",
                    "skipped: brevo not configured",
                )
            else:
                _notify_now = datetime.now(UTC)
                notify_subscribers(
                    db_engine,
                    brevo_api_key=load_brevo_api_key(
                        brevo_config.secrets_file,
                    ),
                    brevo_from_address=brevo_config.sender_email,
                    brevo_from_name=brevo_config.sender_name,
                    cycle_id=_notify_now.strftime("%Y%m%d-%H%M%S"),
                    now=_notify_now,
                    progress_cb=progress_cb,
                )
        else:
            _emit_progress(
                progress_cb, "notify",
                "skipped: today not in backfilled range",
            )

        # Stage 10 (cache) is service-side. No CLI action.
        # ---- Stage 9: fidelity post-flight ----
        # "backfill complete" has always meant "no stage raised", which is not
        # the same as "the data is correct" — DABUR sat in ohlcv_bars with 5 bars
        # from last week, recorded as a clean ETL success, while the industry
        # indices were rebuilt without their largest constituents.
        #
        # This is the stage that closes that gap. It does NOT raise: the data is
        # written and the stages did their work, so refusing to return a result
        # would only hide the verdict. It reports, and the CLI's exit code carries
        # it, so a cron-driven run cannot silently succeed on a half-built
        # database.
        _emit_progress(progress_cb, "stage_9_audit_postflight", "starting")
        _post = audit_exchange(
            db_engine, exchange, gap_days=config.etl.max_coverage_gap_days,
        )
        result_kwargs["fidelity_ready"] = _post.ready
        result_kwargs["fidelity_gaps"] = [d.dataset for d in _post.gaps]
        result_kwargs["fidelity_report"] = _post.render()
        for _line in _post.render().splitlines():
            logger.info("postflight| %s", _line)
        if _post.ready:
            logger.info("postflight: READY — every link in the chain is OK")
        else:
            logger.warning(
                "postflight: NOT READY — %d gap(s) remain after a full "
                "backfill: %s",
                len(_post.gaps), ", ".join(d.dataset for d in _post.gaps),
            )
        _emit_progress(
            progress_cb, "stage_9_audit_postflight",
            "completed: READY" if _post.ready
            else f"completed: {len(_post.gaps)} gap(s) REMAIN",
        )

        _emit_progress(progress_cb, "stage_10_cache", "skipped (service-side)")

    except Exception as e:
        # Per design: log the failure, finish the marker, return a
        # result with failed_stage set. Operator handles cleanup.
        failed_stage = _stage_name_from_traceback()
        error_message = f"{type(e).__name__}: {e}"
        logger.exception(
            "backfill: pipeline failed in %s: %s",
            failed_stage, error_message,
        )
        print(
            f"backfill: pipeline FAILED in {failed_stage}: "
            f"{error_message}",
            file=sys.stderr,
        )
    finally:
        # Always finish the marker.
        try:
            with transaction(db_engine) as conn:
                BackfillRunsRepository(conn).finish(run_id)
        except Exception:
            logger.exception(
                "backfill: FAILED to mark run_id=%d finished — "
                "manual `backfill --release-lock` may be needed",
                run_id,
            )

    finished_at = datetime.now(UTC)
    result_kwargs["finished_at"] = finished_at
    result_kwargs["failed_stage"] = failed_stage
    result_kwargs["error_message"] = error_message
    return BackfillResult(**result_kwargs)


# ============================================================
# Stage helpers
# ============================================================

def _stage_fetch_bars(
    db_engine, config, *,
    exchange: str,
    scoped_tickers: list[int],
    stocks: list[int] | None,
    start_date: date,
    end_date: date,
) -> int:
    """Stage 1: yfinance bar refetch. Returns the etl_runs.run_id
    used for the fetch pass (for cross-reference / debugging).

    When `stocks is None` (full-exchange scope), uses `run_backfill`
    (the existing all-tickers entry point) for the most efficient
    path. When `stocks` is a partial list, uses
    `fetch_bars_for_tickers` (new in Step 11b1) which fetches only
    the listed ticker_ids.
    """
    if stocks is None:
        # Full-exchange path
        return run_backfill(
            db_engine, config,
            exchange=exchange,
            start_date=start_date,
        )
    else:
        return fetch_bars_for_tickers(
            db_engine, config,
            ticker_ids=scoped_tickers,
            start_date=start_date,
            end_date=end_date,
        )


# MERGED-STATE-ENRICHMENT: stages 2 and 3 are one engine pass.
#
# compute_daily_grain_via_engine returns (state_rows, enrich_rows) — it computes
# BOTH every call. The old stage 2 kept only the states and stage 3 RE-FETCHED
# the same bars and RE-RAN the identical engine to keep only the enrichments:
# two full passes over ~13M bars for two halves of one computation.
#
# Stage 2 now writes both tables (state first — stock_indicator_enrichment FKs
# back to it per row). Stage 3 reports the same counts without recomputing, so
# the phase ledger and result_kwargs keep their shape.
#
# Output is byte-identical: same engine, same inputs, same call.
_MERGED_RESULT: dict[str, tuple[int, list[tuple[int, str]]]] = {}


def _stock_state_worker(db_engine, ticker_id: int) -> None:
    """One ticker's merged state+enrichment pass.

    MODULE-LEVEL on purpose: fan_out_processes ships this to child processes,
    and ProcessPoolExecutor pickles callables by qualified name. The closure
    this replaced captured db_engine, which is not picklable — and must not be,
    since a child inheriting the parent's connections is exactly the bug that
    the spawn context exists to prevent. The child passes its OWN engine in.
    """
    populate_stock_state_and_enrichment_for_ticker(
        db_engine, ticker_id, str(Timeframe.WEEKLY),
        mode="full",
    )


def _stage_stock_state_full(
    db_engine, config, scoped_tickers: list[int],
    progress_cb: Callable[[str, str], None] | None = None,
) -> tuple[int, list[tuple[int, str]]]:
    """Stages 2+3: one engine pass writing state AND enrichment.

    Two execution modes, selected by `config.compute.stock_state_processes`:

      >1  (default 3): a PROCESS pool. The merged pass is dominated by
          compute_daily_grain_via_engine — a pure-Python scalar loop holding the
          GIL — so threads serialised it: 8 workers measured 1.15 cores with
          Postgres idle. Processes multiply it. See fan_out_processes.

       1: in-process, on threads. Byte-for-byte the pre-process-pool path. This
          is the ONLY mode in which monkeypatching
          `populate_stock_state_and_enrichment_for_ticker` has any effect —
          spawned children re-import this module clean and never see the
          parent's patches — so orchestration tests that stub the populator must
          select it. It is also the production escape hatch.

    Both modes return (done_count, [(failed_ticker_id, error_str)]) and both
    give per-ticker error isolation, so the phase ledger, _MERGED_RESULT and
    stage 3's no-op cannot tell them apart.
    """
    processes = config.compute.stock_state_processes

    if processes > 1:
        outcome = fan_out_processes(
            scoped_tickers, _stock_state_worker,
            engine_spec=engine_spec_from_config(config),
            label="stage_2_stock_state",
            processes=processes,
            progress_cb=progress_cb,
        )
    else:
        # In-process. db_engine is closed over rather than shipped, which is
        # precisely why this path cannot be parallelised across processes and
        # precisely why monkeypatching works here.
        outcome = fan_out(
            scoped_tickers,
            lambda tid: _stock_state_worker(db_engine, tid),
            label="stage_2_stock_state", progress_cb=progress_cb,
        )

    _MERGED_RESULT["stock"] = (outcome.done, outcome.failed)
    return outcome.done, outcome.failed


def _stage_stock_enrichment_full(
    db_engine, scoped_tickers: list[int],
    progress_cb: Callable[[str, str], None] | None = None,
) -> tuple[int, list[tuple[int, str]]]:
    """Stage 3: already written by stage 2's merged pass — no recompute.

    Reports stage 2's counts so the ledger, progress lines and result_kwargs
    are unchanged. If stage 2 was SKIPPED by the resume ledger, this returns
    a zero result for the same reason stage 2 did: the rows already exist.
    """
    done, failed = _MERGED_RESULT.get(
        "stock", (len(scoped_tickers), []),
    )
    return done, failed


def _stage_market_cap(
    db_engine: Engine, *, exchange: str, date_from: date, date_to: date,
    progress_cb: Callable[[str, str], None] | None = None,
) -> int:
    """Populate daily_market_cap for the exchange over the range
    (adjusted close x current shares). Returns approx rows."""
    res = populate_market_cap_range(
        db_engine, exchange,
        date_from=date_from, date_to=date_to,
        use_current_shares_always=True,
        progress_cb=progress_cb,
    )
    if not isinstance(res, dict):
        return 0
    # Total mcap rows written = exact (computed) + approximate (approx).
    n_rows = int(res.get("computed", 0)) + int(res.get("approx", 0))
    logger.info(
        "market_cap: %d rows (computed=%d, approx=%d, "
        "no_close=%d, no_shares=%d)",
        n_rows, res.get("computed", 0), res.get("approx", 0),
        res.get("skipped_no_close", 0), res.get("skipped_no_shares", 0),
    )
    return n_rows


def _stage_industry_index_build(
    db_engine: Engine, *, exchange: str, through: date,
    progress_cb: Callable[[str, str], None] | None = None,
) -> int:
    """Build per-exchange annual-April chain-linked industry indices
    (top 12, min 4).

    Visible-tolerance (Option 3): the phase completes even if some
    industries were skipped (too few tickers — structural, not a failure)
    or failed (a real error). Both are LOGGED (log file + terminal, since
    the CLI streams logger output), so nothing is lost silently. Failed
    industries are named so they can be chased. Returns chains built.
    """
    out = build_all_industry_index_chains(
        db_engine, exchange, through=through, n=12, min_constituents=4,
        progress_cb=progress_cb,
    )
    if not isinstance(out, dict):
        return 0
    n_built = len(out.get("chains", []))
    unqualified = out.get("unqualified", [])
    failed = out.get("failed", [])
    logger.info(
        "industry_index_build: built %d, skipped %d (insufficient "
        "tickers), failed %d",
        n_built, len(unqualified), len(failed),
    )
    if failed:
        _names = ", ".join(str(f[0]) for f in failed)
        logger.warning(
            "industry_index_build: %d industries FAILED (real errors, "
            "not structural): %s", len(failed), _names,
        )
    return n_built


def _stage_industry_index_values(
    db_engine: Engine, *, date_from: date, date_to: date,
    progress_cb: Callable[[str, str], None] | None = None,
) -> int:
    """Backfill daily industry_index_value rows for all active indices.

    Routes through the SHARED populator the live intraday/eod cycle also
    uses (populate_industry_index_value_for_all), so all three pipelines
    compute index values through one code path and differ only in the
    date window (full range here, trailing weeks live). Resume/force is
    handled at the phase-ledger boundary, so no per-industry skip-recent
    guard is needed here. Returns the count of industries computed OK.
    """
    report = populate_industry_index_value_for_all(
        db_engine, from_date=date_from, to_date=date_to,
        progress_cb=progress_cb,
    )
    return (
        len(report.get("succeeded", []))
        if isinstance(report, dict) else 0
    )


def _stage_delete_historical(
    db_engine, *,
    scoped_tickers: list[int],
    from_monday: date,
    to_date: date,
    exchange: str,
) -> int:
    """Stage 7: DELETE historical_signals for scoped tickers with
    computed_at in [local-midnight(from_monday), local-midnight(to_date + 1)).

    DELETE-UPPER-BOUND: bounding the upper edge to the walk's date_to is
    required — the daywalk only regenerates [date_from, date_to], so an
    unbounded delete would leave a hole of missing signals in
    (date_to, today] on any bounded (to_date < today) run.

    L2-ZONE-IS-NAMED: both edges were UTC, and the upper one was the closed
    `23:59:59.999999` form. computed_at is stamped at post-session LOCAL time, so
    for any market far enough WEST the last day's signals land after midnight UTC,
    fall outside the closed UTC window, survive the delete — and the rebuild then
    inserts alongside them. Half-open, market-local bounds cannot do that."""
    from_dt = week_anchor(from_monday, exchange)
    _, to_dt = day_bounds(to_date, exchange)
    with transaction(db_engine) as conn:
        return HistoricalSignalsRepository(
            conn,
        ).delete_for_tickers_from_date(
            ticker_ids=scoped_tickers,
            from_date=from_dt,
            to_date=to_dt,
        )


def _stage_benchmark_stats(
    db_engine,
    *,
    exchange: str,
    progress_cb: Callable[[str, str], None] | None = None,
) -> tuple[int, int, int, int, list[tuple[str, str]]]:
    """BENCHMARK-STATS-STAGE: stock + industry benchmark stats.

    Writes stock_benchmark_stats (each ticker vs its INDUSTRY index and vs
    NIFTY) and industry_benchmark_stats (each industry vs NIFTY), across the
    SHORT / MEDIUM / LONG windows. Idempotent on
    (ticker|industry, benchmark, window, as_of_date).

    These feed `rank_score`, which orders the notification email. Before this
    stage existed they were populated only by a manual CLI command, so on a
    fresh backfill both tables were empty and every signal ranked 0.0.

    Returns (tickers_done, tickers_skipped, industries_done, industries_skipped,
    failed). SKIPPED IS NORMAL — a ticker in a sub-threshold industry (no
    industry_index_value rows) has no INDUSTRY benchmark, and NIFTYBEES has no
    NIFTY benchmark (self-vs-self). Both runners isolate per-item errors, so one
    bad ticker cannot take the stage down.
    """
    _emit_progress(progress_cb, "stage_6d_benchmark_stats", "tickers")
    t = run_benchmark_stats_for_all_tickers(db_engine, exchange=exchange)

    _emit_progress(progress_cb, "stage_6d_benchmark_stats", "industries")
    i = run_benchmark_stats_for_all_industries(db_engine)

    failed: list[tuple[str, str]] = []
    for name, err in t.get("failed", []):
        failed.append((f"ticker:{name}", str(err)))
    for name, err in i.get("failed", []):
        failed.append((f"industry:{name}", str(err)))

    n_t_ok = len(t.get("succeeded", []))
    n_t_skip = len(t.get("skipped", []))
    n_i_ok = len(i.get("succeeded", []))
    n_i_skip = len(i.get("skipped", []))

    logger.info(
        "stage_6d benchmark_stats: tickers %d ok / %d skipped, "
        "industries %d ok / %d skipped, %d failed",
        n_t_ok, n_t_skip, n_i_ok, n_i_skip, len(failed),
    )
    if failed:
        logger.warning(
            "stage_6d benchmark_stats: %d item(s) FAILED (first 5: %s)",
            len(failed), failed[:5],
        )
    return n_t_ok, n_t_skip, n_i_ok, n_i_skip, failed


# ============================================================
# Helpers
# ============================================================

def _resolve_tickers(
    db_engine, exchange: str, stocks: list[int] | None,
) -> list[int]:
    """Resolve scope to a concrete list of ticker_ids. None means
    'all active tickers on exchange'."""
    with db_engine.connect() as conn:
        if stocks is None:
            rows = conn.execute(
                text("""
                    SELECT ticker_id FROM tickers
                     WHERE exchange = :ex
                       AND is_active = TRUE
                       AND delisted_at IS NULL
                     ORDER BY ticker_id
                """),
                {"ex": exchange},
            ).all()
            return [r.ticker_id for r in rows]
        else:
            # Validate that all requested tickers exist on the
            # exchange + are active. Silently drops bad ids would
            # be a footgun.
            rows = conn.execute(
                text("""
                    SELECT ticker_id FROM tickers
                     WHERE ticker_id = ANY(:ids)
                       AND exchange = :ex
                       AND is_active = TRUE
                       AND delisted_at IS NULL
                     ORDER BY ticker_id
                """),
                {"ids": stocks, "ex": exchange},
            ).all()
            found = {r.ticker_id for r in rows}
            missing = set(stocks) - found
            if missing:
                raise ValueError(
                    f"ticker_ids not found / inactive / wrong "
                    f"exchange: {sorted(missing)}",
                )
            return sorted(found)


def _monday_of(d: date) -> date:
    """Monday of the local week containing `d`.

    L1-ANCHOR-RULE: delegates. This file, backfill_queries.py and
    indicators/aggregation.py each carried their own copy, two of them documented
    as "mirrors the other one" — which is a comment, not a mechanism. The
    duplicates are gone; the fact has one home.
    """
    return monday_of(d)   # L4-FIX-FORWARD: the alias was never needed


def _emit_progress(
    cb: Callable[[str, str], None] | None,
    stage: str, message: str,
) -> None:
    if cb is not None:
        cb(stage, message)


def _stage_name_from_traceback() -> str:
    """Best-effort: find the outermost `_stage_*` frame in the
    in-flight exception's traceback. Falls back to 'unknown'
    when no exception is in flight or no _stage_* frame is on
    the chain.

    Uses sys.exc_info()[2] (the exception's traceback) — NOT
    traceback.extract_stack() — because the latter would include
    the except handler's own call frame (this function itself),
    making it self-referential.
    """
    import sys
    import traceback
    _, _, exc_tb = sys.exc_info()
    if exc_tb is None:
        return "unknown"
    for frame in traceback.extract_tb(exc_tb):
        if frame.name.startswith("_stage_"):
            return frame.name.removeprefix("_")
    return "unknown"
