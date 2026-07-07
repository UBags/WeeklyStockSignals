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
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ..domain import Timeframe
from ..domain.signal_event import SignalEvent
from ..etl.runner import fetch_bars_for_tickers, run_backfill
from ..fundamentals.v1_runners import (
    populate_industry_index_value_for_all,
    populate_stock_indicator_enrichment_for_ticker,
    populate_stock_indicator_state_for_ticker,
)
from ..orchestrator.derive_signals import (
    derive_signal_for_ticker_this_cycle,
)
from ..orchestrator.process_day import process_signals_for_day
from ..orchestrator.industry_stages import (
    _stage_industry_enrichment,
    _stage_industry_state,
)
from ..persistence import transaction
from ..persistence.backfill_runs_repo import BackfillRunsRepository
from ..persistence.current_signals_repo import CurrentSignalsRepository
from ..persistence.historical_signals_repo import (
    HistoricalSignalsRepository,
)

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


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

def backfill(
    db_engine: Engine,
    config,
    *,
    date_from: date,
    date_to: date | None = None,
    exchange: str = "NSE",
    stocks: list[int] | None = None,
    refetch_bars: bool = True,
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
        date_to = date.today()
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
        # ---- Stage 1: yfinance bar refetch ----
        if refetch_bars:
            _emit_progress(progress_cb, "stage_1_fetch_bars", "starting")
            result_kwargs["bars_fetch_run_id"] = _stage_fetch_bars(
                db_engine, config,
                exchange=exchange,
                scoped_tickers=scoped_tickers,
                stocks=stocks,
                start_date=date_from,
                end_date=date_to,
            )
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
        done, failed = _stage_stock_state_full(
            db_engine, scoped_tickers, progress_cb,
        )
        _emit_progress(
            progress_cb, "stage_2_stock_state",
            f"completed: {done}/{len(scoped_tickers)} stocks"
            + (f" ({len(failed)} failed)" if failed else ""),
        )
        result_kwargs["stock_state_tickers_done"] = done
        result_kwargs["stock_state_failed"] = failed

        # ---- Stage 3: stock indicator enrichment ----
        _emit_progress(
            progress_cb, "stage_3_stock_enrichment",
            f"starting ({len(scoped_tickers)} stocks)",
        )
        done, failed = _stage_stock_enrichment_full(
            db_engine, scoped_tickers, progress_cb,
        )
        _emit_progress(
            progress_cb, "stage_3_stock_enrichment",
            f"completed: {done}/{len(scoped_tickers)} stocks"
            + (f" ({len(failed)} failed)" if failed else ""),
        )
        result_kwargs["stock_enrichment_tickers_done"] = done
        result_kwargs["stock_enrichment_failed"] = failed

        # ---- Stage 4: IIV refresh ----
        _emit_progress(progress_cb, "stage_4_iiv", "starting")
        iiv_done = _stage_iiv_full(
            db_engine, date_from=date_from, date_to=date_to,
        )
        _emit_progress(
            progress_cb, "stage_4_iiv",
            f"completed: {iiv_done} industries",
        )
        result_kwargs["iiv_industries_done"] = iiv_done

        # ---- Stage 5: industry indicator state ----
        _emit_progress(progress_cb, "stage_5_industry_state", "starting")
        ind_state_done = _stage_industry_state(db_engine, mode="full")[0]
        _emit_progress(
            progress_cb, "stage_5_industry_state",
            f"completed: {ind_state_done} indicators",
        )
        result_kwargs["industry_state_done"] = ind_state_done

        # ---- Stage 6: industry indicator enrichment ----
        _emit_progress(
            progress_cb, "stage_6_industry_enrichment", "starting",
        )
        ind_enr_done = _stage_industry_enrichment(db_engine, mode="full")[0]
        _emit_progress(
            progress_cb, "stage_6_industry_enrichment",
            f"completed: {ind_enr_done} indicators",
        )
        result_kwargs["industry_enrichment_done"] = ind_enr_done

        # ---- Stage 7: DELETE historical_signals in range ----
        _emit_progress(
            progress_cb, "stage_7_delete_historical", "starting",
        )
        result_kwargs["historical_signals_deleted"] = (
            _stage_delete_historical(
                db_engine,
                scoped_tickers=scoped_tickers,
                from_monday=from_monday,
            )
        )

        # ---- Stages 8+9: per-day walk on the unified wrapper ----
        # Each trading day D in the range runs process_signals_for_day in
        # backfill mode: per-day historical (delete D + insert) and current
        # only when D == today (the final day). Replaces the legacy Monday-
        # walk + separate current rebuild.
        _emit_progress(
            progress_cb, "stage_8_walk_days", "starting",
        )
        _today = datetime.now(_IST).date()
        (days_walked, inserted, upserted, deleted,
         today_walked) = _stage_backfill_daywalk(
            db_engine,
            scoped_tickers=scoped_tickers,
            date_from=date_from,
            date_to=date_to,
            today=_today,
        )
        result_kwargs["days_walked"] = days_walked
        result_kwargs["historical_signals_inserted"] = inserted
        result_kwargs["current_signals_upserted"] = upserted
        result_kwargs["current_signals_deleted"] = deleted

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


def _stage_stock_state_full(
    db_engine, scoped_tickers: list[int],
    progress_cb: "Callable[[str, str], None] | None" = None,
) -> tuple[int, list[tuple[int, str]]]:
    """Stage 2: full recompute of stock_indicator_state per ticker.

    Returns (done_count, [(failed_ticker_id, error_str)]).
    Per-ticker error isolation — a failure on ticker X doesn't
    abort the rest. Emits "N/total stocks done" via progress_cb
    roughly every 1% of the ticker list.
    """
    done = 0
    failed: list[tuple[int, str]] = []
    total = len(scoped_tickers)
    every = max(1, total // 100)
    for i, tid in enumerate(scoped_tickers, 1):
        try:
            populate_stock_indicator_state_for_ticker(
                db_engine, tid, str(Timeframe.WEEKLY),
                mode="full",
            )
            done += 1
        except Exception as e:
            failed.append((tid, f"{type(e).__name__}: {e}"))
            logger.warning(
                "backfill stage_2: ticker_id=%d FAILED: %s",
                tid, e,
            )
        if i % every == 0 or i == total:
            _emit_progress(
                progress_cb, "stage_2_stock_state",
                f"{done}/{total} stocks done"
                + (f" ({len(failed)} failed)" if failed else ""),
            )
    return done, failed


def _stage_stock_enrichment_full(
    db_engine, scoped_tickers: list[int],
    progress_cb: "Callable[[str, str], None] | None" = None,
) -> tuple[int, list[tuple[int, str]]]:
    """Stage 3: full recompute of stock_indicator_enrichment.

    Emits "N/total stocks done" via progress_cb ~every 1%."""
    done = 0
    failed: list[tuple[int, str]] = []
    total = len(scoped_tickers)
    every = max(1, total // 100)
    for i, tid in enumerate(scoped_tickers, 1):
        try:
            populate_stock_indicator_enrichment_for_ticker(
                db_engine, tid, str(Timeframe.WEEKLY),
                mode="full",
            )
            done += 1
        except Exception as e:
            failed.append((tid, f"{type(e).__name__}: {e}"))
            logger.warning(
                "backfill stage_3: ticker_id=%d FAILED: %s",
                tid, e,
            )
        if i % every == 0 or i == total:
            _emit_progress(
                progress_cb, "stage_3_stock_enrichment",
                f"{done}/{total} stocks done"
                + (f" ({len(failed)} failed)" if failed else ""),
            )
    return done, failed


def _stage_iiv_full(
    db_engine, *, date_from: date, date_to: date,
) -> int:
    """Stage 4: IIV refresh for all industries, full date range."""
    report = populate_industry_index_value_for_all(
        db_engine, from_date=date_from, to_date=date_to,
    )
    return len(report.get("succeeded", []))


def _stage_delete_historical(
    db_engine, *,
    scoped_tickers: list[int],
    from_monday: date,
) -> int:
    """Stage 7: DELETE historical_signals for scoped tickers where
    `computed_at >= from_monday`. Uses the new
    `delete_for_tickers_from_date` repo method."""
    from_dt = datetime.combine(
        from_monday, time(0, 0), tzinfo=UTC,
    )
    with transaction(db_engine) as conn:
        return HistoricalSignalsRepository(
            conn,
        ).delete_for_tickers_from_date(
            ticker_ids=scoped_tickers,
            from_date=from_dt,
        )


def _walk_trading_days(
    db_engine, *, scoped_tickers: list[int],
    date_from: date, date_to: date,
) -> list[date]:
    """Trading days in [date_from, date_to] for the scoped tickers = the
    distinct stock_indicator_state dates in range (populated by the state
    stage earlier in this pipeline). Only days with state can produce
    signals, so this is both correct and the minimal set to walk."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT date
                  FROM stock_indicator_state
                 WHERE date BETWEEN :f AND :t
                   AND timeframe = :tf
                   AND ticker_id = ANY(:ids)
                 ORDER BY date
            """),
            {"f": date_from, "t": date_to, "tf": "1W", "ids": scoped_tickers},
        ).all()
    return [r[0] for r in rows]


def _stage_backfill_daywalk(
    db_engine, *, scoped_tickers: list[int],
    date_from: date, date_to: date, today: date,
) -> tuple[int, int, int, int, bool]:
    """Stages 8+9 unified: walk each trading day in range and run
    process_signals_for_day in backfill mode. Per-day historical
    (delete day + insert); current only on the day that == today.

    Returns (days_walked, historical_inserted, current_upserted,
    current_deleted, today_walked). today_walked is True iff today's
    trading day was in the walked range (drives the notify gate)."""
    days = _walk_trading_days(
        db_engine, scoped_tickers=scoped_tickers,
        date_from=date_from, date_to=date_to,
    )
    hist_ins = cur_up = cur_del = 0
    for d in days:
        as_of_dt = datetime.combine(d, time(16, 30), tzinfo=_IST)
        pd = process_signals_for_day(
            db_engine,
            scoped_tickers=scoped_tickers,
            day=d,
            today=today,
            mode="backfill",
            as_of=as_of_dt,
        )
        hist_ins += pd.historical_written
        cur_up += pd.current_upserted
        cur_del += pd.current_deleted
    return len(days), hist_ins, cur_up, cur_del, (today in days)


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
    """Monday of the week containing `d`. Matches the IST-Monday
    anchor used throughout the V1 weekly bars."""
    return d - timedelta(days=d.weekday())


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
