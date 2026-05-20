"""Historical signal generation for backtesting (Phase 15a).

Reads `ohlcv_bars` for each active ticker on an exchange, replays
through a fresh `TickerEngine` per (ticker, timeframe), captures
every `ChangeSignal != NO_CHANGE` event, and merges results into a
single JSON file per exchange.

Two execution modes:
  - **CLI / synchronous**: `generate_historical_signals(..., run_id=None)`
    creates and closes its own `etl_runs` row.
  - **HTTP / async (Phase 15b)**: the HTTP route opens the etl_runs
    row in the request thread (for the concurrent-run guard) and
    passes the run_id to the function, which then closes the row
    on completion. This split exists because the guard query has
    to see the row before the worker thread accepts the work.

Resume semantics:
  For each (ticker_id, timeframe), the runner consults
  `signal_backtest_runs`. If a row exists, replay starts from
  `(computed_through_date + 1 day)`. If no row exists, replay
  starts from the ticker's earliest bar. `force_full=true` wipes
  resume rows for the exchange in a small dedicated transaction at
  run start, then replay proceeds as if no resume rows existed.

Priming:
  The engine needs ~longest_base + longest_s2 + longest_final
  bars to settle on weekly (≈36 weekly bars ≈ 180 daily bars). We
  prime with `_PRIMING_DAILY_BARS` (250) bars of headroom from the
  database before `start_date`. Signals emitted during priming are
  discarded by the `bar.date >= start_date` filter. For force-full
  runs (no prior `computed_through_date`), the first 250 daily
  bars of a ticker's history are consumed for priming and their
  signals are discarded — the JSON output's first signal for that
  ticker is therefore ~year 1 of its history, not day 1. This is
  intentional (you can't compute a meaningful EMA from one bar)
  and is documented in the JSON schema notes.

Merge protocol:
  At run start, load existing JSON into memory (empty dict if
  missing). For each ticker successfully processed, the runner
  calls `_merge_signals_into` with the recomputed window and the
  list of new signals. The merge logic handles three transitions:
    - date had no prior signal, engine emits one now → added
    - date had a prior signal, engine emits different/same → replaced
    - date had a prior signal, engine emits none now → removed
  Dates outside the recomputed window are untouched. Tickers not
  re-processed (e.g., now-inactive ones) keep their old signals.

Atomic write:
  At run end, the in-memory dict is serialized to
  `generated_signals/{EXCHANGE}.json.tmp`, fsync'd, then renamed
  to `generated_signals/{EXCHANGE}.json`. POSIX guarantees rename
  atomicity. A reader opening the file mid-write sees either the
  old or the new version, never a half-written one.

Not in scope (Phase 15a):
  - Cancellation of in-flight runs (deferred to Phase 18).
  - Multi-exchange aggregation (one JSON per exchange — D5).
  - Live engine using exchange_overrides (deferred — OOS-7).
  - Modifying signal_events or any other live-engine table (N6).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..config import Config
from ..config.exchange_overrides import resolve_indicator_params
from ..domain import (
    ChangeSignal,
    CoreRegime,
    IndicatorParams,
    OHLCVBar,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from ..engine.change_classifier import classify_change
from ..engine.ticker_engine import TickerEngine
from ..etl.exchanges import validate_exchange
from ..etl.status_repo import EtlStatusRepository
from ..etl.types import RunKind, RunOutcome
from ..indicators.aggregation import daily_to_weekly
from ..observability.correlation import correlation_id, new_correlation_id
from ..persistence.bars_repo import BarsRepository
from ..persistence.signal_backtest_runs_repo import SignalBacktestRunsRepository

logger = logging.getLogger(__name__)


# ---------- Tunables ---------- #


# How many daily bars of priming we feed the engine before
# `start_date` so the EMA / ATR recurrences are settled by the time
# we start capturing emitted signals.
#
# Calibration: the engine's longest EMA window is
#     longest_base + longest_s2 + (longest_final or 0) ≈ 28+5+3 = 36
# weekly bars. At ~5 trading days per weekly bar, that's 180 daily
# bars to feed the weekly aggregator. Add headroom for volume_sma
# (2 bars) and ATR (14 bars) — round up to 250. This is intentionally
# generous; the cost of extra priming is a few ms per ticker, but the
# cost of a too-short prime is wrong signals on the leading edge of
# the captured window.
_PRIMING_DAILY_BARS = 250


# The on-disk schema version. Bump on incompatible changes to the
# JSON file format (per D5 / §4.3).
_SCHEMA_VERSION = "1.0"


# Default output directory for generated JSON files. Relative to
# CWD at runner invocation, mirroring the convention used by
# `config.etl.parked_dir` and `config.etl.checkpoint_dir`. Tests
# override via `output_dir` kwarg.
_DEFAULT_OUTPUT_DIR = Path("generated_signals")


# ---------- Result type ---------- #


@dataclass(frozen=True)
class SignalGenerationResult:
    """Summary returned by `generate_historical_signals`."""
    exchange: str
    run_id: int
    output_path: Path
    timeframes_computed: list[str]
    tickers_processed: int
    tickers_skipped: int          # had no new bars beyond computed_through_date
    signals_emitted: dict[str, int]   # by signal_type, e.g. {"BUY": 4513, ...}
    signals_deleted: int          # merge: prior records removed
    started_at: datetime
    finished_at: datetime

    def summary_line(self) -> str:
        """Single-line summary for the CLI."""
        emitted_total = sum(self.signals_emitted.values())
        return (
            f"signal_backtest complete: exchange={self.exchange} "
            f"run_id={self.run_id} "
            f"timeframes={self.timeframes_computed} "
            f"tickers_processed={self.tickers_processed} "
            f"tickers_skipped={self.tickers_skipped} "
            f"signals_emitted={emitted_total} "
            f"({self.signals_emitted}) "
            f"signals_deleted_by_merge={self.signals_deleted} "
            f"output={self.output_path}"
        )


# ---------- Public entry point ---------- #


def generate_historical_signals(
    db_engine: Engine,
    config: Config,
    *,
    exchange: str,
    timeframes: Iterable[Timeframe],
    force_full: bool = False,
    run_id: Optional[int] = None,
    output_dir: Optional[Path] = None,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
) -> SignalGenerationResult:
    """Recompute historical signals for an entire exchange across the
    given timeframes and write the per-exchange JSON file.

    Args:
        db_engine: SQLAlchemy Engine.
        config: validated Config; consulted for market_hours (tz
            lookup), exchange_overrides (indicator params), and
            etl.parked_dir-style conventions.
        exchange: exchange code to process (e.g. "NSE"). Validated
            against `config.market_hours`; UnsupportedExchangeError
            propagates if unknown.
        timeframes: which timeframes to compute. Phase 15 supports
            "1D" and "1W" only.
        force_full: if True, wipe `signal_backtest_runs` rows for the
            exchange in a small dedicated transaction at run start,
            then recompute every ticker from its earliest bar. If
            False (default), resume from each ticker's prior
            `computed_through_date + 1 day`.
        run_id: if provided, use this etl_runs row (the caller — Phase
            15b's HTTP route — opened it for the concurrent-run guard
            and owns close-on-failure as a fallback). If None (the CLI
            case), this function opens AND closes the row itself.
            Asymmetric ownership documented on
            `EtlStatusRepository.open_run`.
        output_dir: where to write the JSON file. Defaults to
            `./generated_signals/`. Tests pass tmp_path.
        progress_callback: optional `(stage, current, total) -> None`
            hook for long-running operations. Phase 15a doesn't wire
            it through the GUI yet; Phase 15c may.

    Returns:
        SignalGenerationResult with run_id, file path, and counts.

    Raises:
        UnsupportedExchangeError: unknown / unconfigured exchange.
        Any DB / IO error propagates after closing the etl_runs row
        with outcome='failed' (if this function owns the row).
    """
    # --- 1. Validate inputs --- #
    validate_exchange(exchange, config)

    tfs = list(timeframes)
    if not tfs:
        raise ValueError(
            "generate_historical_signals: timeframes must not be empty"
        )
    from ..config.exchange_overrides import SUPPORTED_BACKTEST_TIMEFRAMES
    for tf in tfs:
        if tf.value not in SUPPORTED_BACKTEST_TIMEFRAMES:
            raise ValueError(
                f"generate_historical_signals: timeframe {tf.value!r} not "
                f"supported in Phase 15. Supported: "
                f"{sorted(SUPPORTED_BACKTEST_TIMEFRAMES)}."
            )

    out_dir = output_dir if output_dir is not None else _DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{exchange}.json"

    cid = new_correlation_id()
    started_at = datetime.now(timezone.utc)
    owns_run_lifecycle = run_id is None

    with correlation_id(cid):
        # --- 2. Open the etl_runs row if we own its lifecycle --- #
        if owns_run_lifecycle:
            with db_engine.begin() as conn:
                run_id = EtlStatusRepository(conn).open_run(
                    RunKind.SIGNAL_BACKTEST,
                    exchange=exchange,
                    correlation_id=cid,
                    notes=(
                        f"force_full={force_full}, "
                        f"timeframes={[t.value for t in tfs]}"
                    ),
                )
        # `run_id` is now non-None either way; reassure the type system.
        assert run_id is not None

        # Track outcome so the `finally` block can close the row
        # correctly. Default to failed; flipped to success at the end.
        outcome = RunOutcome.FAILED
        close_notes: Optional[str] = None

        try:
            result = _do_generate(
                db_engine=db_engine,
                config=config,
                exchange=exchange,
                timeframes=tfs,
                force_full=force_full,
                run_id=run_id,
                output_path=output_path,
                started_at=started_at,
                progress_callback=progress_callback,
            )
            outcome = RunOutcome.SUCCESS
            close_notes = (
                f"tickers_processed={result.tickers_processed}, "
                f"tickers_skipped={result.tickers_skipped}, "
                f"signals_emitted={sum(result.signals_emitted.values())}"
            )
            return result
        except Exception:
            logger.exception(
                "signal_backtest run failed",
                extra={"run_id": run_id, "exchange": exchange},
            )
            raise
        finally:
            # Close the etl_runs row if and only if we own the
            # lifecycle. Phase 15b's HTTP route owns close-on-success
            # via the same path it called us with; if WE raise, the
            # 15b code path catches us at the executor's `submit` and
            # closes with outcome='failed' itself. The
            # owns_run_lifecycle flag keeps the contract clear.
            if owns_run_lifecycle:
                try:
                    with db_engine.begin() as conn:
                        EtlStatusRepository(conn).close_run(
                            run_id, outcome, notes=close_notes,
                        )
                except Exception:
                    # Already-failing path; log and swallow so the
                    # original exception propagates.
                    logger.exception(
                        "Failed to close etl_runs row",
                        extra={"run_id": run_id, "exchange": exchange},
                    )


# ---------- Inner pipeline ---------- #


def _do_generate(
    *,
    db_engine: Engine,
    config: Config,
    exchange: str,
    timeframes: list[Timeframe],
    force_full: bool,
    run_id: int,
    output_path: Path,
    started_at: datetime,
    progress_callback: Optional[Callable[[str, int, int], None]],
) -> SignalGenerationResult:
    """Pure-pipeline implementation; the public entry point handles
    run_id lifecycle around this. Separated so the lifecycle code
    doesn't drown the algorithm."""

    t0 = time.monotonic()

    # --- 1. Force-full: wipe resume rows in a dedicated transaction --- #
    if force_full:
        with db_engine.begin() as conn:
            deleted = SignalBacktestRunsRepository(conn).delete_by_exchange(
                exchange,
            )
        logger.info(
            "signal_backtest: force_full wiped resume rows",
            extra={"exchange": exchange, "rows_deleted": deleted},
        )

    # --- 2. Look up the exchange's local timezone --- #
    tz = _exchange_tz(config, exchange)

    # --- 3. Load the existing JSON file (or start empty) --- #
    in_memory = _load_existing_or_empty(output_path, exchange)
    # We rebuild the params block fresh from the resolved params
    # used in THIS run; that captures the latest tuning.
    resolved_params: dict[Timeframe, IndicatorParams] = {
        tf: resolve_indicator_params(config, exchange, tf) for tf in timeframes
    }
    in_memory["params"] = {
        tf.value: _params_to_dict(p) for tf, p in resolved_params.items()
    }

    # --- 4. Fetch the active tickers for the exchange --- #
    tickers = _fetch_active_tickers(db_engine, exchange)
    logger.info(
        "signal_backtest: starting",
        extra={
            "exchange": exchange,
            "ticker_count": len(tickers),
            "timeframes": [t.value for t in timeframes],
            "force_full": force_full,
            "run_id": run_id,
        },
    )

    tickers_processed = 0
    tickers_skipped = 0
    signals_deleted_total = 0
    # Counts for all NEW signals emitted in THIS run (across all
    # ticker × timeframe combinations). Doesn't include signals
    # that were already in the file from previous runs.
    signals_emitted: dict[str, int] = {
        ChangeSignal.BUY.name: 0,
        ChangeSignal.SELL.name: 0,
        ChangeSignal.EXIT_FROM_BUY.name: 0,
        ChangeSignal.EXIT_FROM_SELL.name: 0,
    }

    # --- 5. Per-ticker loop --- #
    for i, t in enumerate(tickers):
        if progress_callback is not None:
            try:
                progress_callback("tickers", i + 1, len(tickers))
            except Exception:
                # A buggy callback shouldn't kill the run.
                logger.debug(
                    "progress_callback raised", exc_info=True,
                )

        ticker_did_work = False
        for tf in timeframes:
            params = resolved_params[tf]
            window = _process_ticker_timeframe(
                db_engine=db_engine,
                exchange=exchange,
                ticker=t,
                timeframe=tf,
                params=params,
                tz=tz,
                force_full=force_full,
                in_memory=in_memory,
                run_id=run_id,
                emitted_counts=signals_emitted,
            )
            if window is not None:
                signals_deleted_total += window.deleted_in_merge
                ticker_did_work = True

        if ticker_did_work:
            tickers_processed += 1
        else:
            tickers_skipped += 1

    # --- 6. Refresh counts metadata and atomic write --- #
    finished_at = datetime.now(timezone.utc)
    _refresh_counts(in_memory)
    in_memory["generated_at"] = finished_at.isoformat()
    in_memory["generated_by_run_id"] = run_id
    in_memory["schema_version"] = _SCHEMA_VERSION
    in_memory["exchange"] = exchange

    _atomic_write_json(output_path, in_memory)

    logger.info(
        "signal_backtest: complete",
        extra={
            "exchange": exchange,
            "run_id": run_id,
            "duration_seconds": time.monotonic() - t0,
            "output_path": str(output_path),
            "tickers_processed": tickers_processed,
            "tickers_skipped": tickers_skipped,
        },
    )

    return SignalGenerationResult(
        exchange=exchange,
        run_id=run_id,
        output_path=output_path,
        timeframes_computed=[t.value for t in timeframes],
        tickers_processed=tickers_processed,
        tickers_skipped=tickers_skipped,
        signals_emitted=signals_emitted,
        signals_deleted=signals_deleted_total,
        started_at=started_at,
        finished_at=finished_at,
    )


# ---------- Per-ticker × timeframe processing ---------- #


@dataclass
class _ActiveTicker:
    """Slim ticker info used inside the runner. Distinct from
    BarsRepository's records because we only need a handful of
    fields and want clear types."""
    ticker_id: int
    ref: TickerRef
    # The exchange's tz string for daily_to_weekly bucket-anchor
    # decisions. Could be on the ticker or on the exchange — we
    # take it from market_hours per design.


@dataclass(frozen=True)
class _RecomputedWindow:
    """What replay actually did for one (ticker, timeframe), so the
    merge step can know what to overwrite."""
    start_date: date
    end_date: date
    deleted_in_merge: int


def _process_ticker_timeframe(
    *,
    db_engine: Engine,
    exchange: str,
    ticker: _ActiveTicker,
    timeframe: Timeframe,
    params: IndicatorParams,
    tz: str,
    force_full: bool,
    in_memory: dict,
    run_id: int,
    emitted_counts: dict[str, int],
) -> Optional[_RecomputedWindow]:
    """Run the replay for one (ticker, timeframe). Returns None if
    the ticker had no new bars to compute; otherwise the recomputed
    window for caller's bookkeeping.

    Per-ticker commit model: at the end of a successful run, this
    function opens a transaction and upserts the resume row. If the
    overall run dies after this ticker but before others, the resume
    row for this ticker is durable and the next run resumes from here.
    """
    # --- 1. Resume check --- #
    with db_engine.connect() as conn:
        resume_repo = SignalBacktestRunsRepository(conn)
        existing = resume_repo.get(
            exchange=exchange,
            ticker_id=ticker.ticker_id,
            timeframe=timeframe,
        )

    if force_full or existing is None:
        # No resume row — start from the ticker's earliest bar.
        # We let the bar fetch handle "earliest" by not filtering;
        # `start_date` is determined after we look at the bars.
        start_date: Optional[date] = None
    else:
        # Resume: pick up the day after the previous computed_through.
        start_date = existing.computed_through_date + _one_day()

    # --- 2. Fetch the source bars (always daily; weekly is synthesized) --- #
    # We always need daily bars even for the weekly path, because
    # weekly is synthesized in-memory via daily_to_weekly.
    with db_engine.connect() as conn:
        bars_repo = BarsRepository(conn)
        # We need ALL daily bars for the ticker — fetch_history takes
        # `limit` as "most recent N", so we use a generous limit. For a
        # 20-year history at ~250 trading days/year, 6000 is a comfortable
        # ceiling.
        bars_1d = bars_repo.fetch_history(
            ticker.ticker_id, Timeframe.DAILY, limit=10_000,
        )

    if not bars_1d:
        logger.debug(
            "signal_backtest: no daily bars for ticker",
            extra={
                "ticker_id": ticker.ticker_id,
                "symbol": ticker.ref.symbol,
                "timeframe": timeframe.value,
            },
        )
        return None

    earliest_bar_date = bars_1d[0].timestamp.date()
    latest_bar_date = bars_1d[-1].timestamp.date()

    # If we have a resume start and the latest bar is older than it,
    # there's nothing new to compute. Skip.
    if start_date is not None and latest_bar_date < start_date:
        logger.debug(
            "signal_backtest: nothing new for ticker",
            extra={
                "ticker_id": ticker.ticker_id,
                "symbol": ticker.ref.symbol,
                "timeframe": timeframe.value,
                "resume_start": start_date.isoformat(),
                "latest_bar": latest_bar_date.isoformat(),
            },
        )
        return None

    # If no resume row (or force_full), start_date is the earliest
    # bar date plus _PRIMING_DAILY_BARS (priming is discarded by the
    # bar.date >= start_date filter). We compute the *effective*
    # start_date here so that capture begins after priming.
    if start_date is None:
        # Pick the date of the bar that's `_PRIMING_DAILY_BARS`
        # positions in (clamped to the available history).
        prime_idx = min(_PRIMING_DAILY_BARS, len(bars_1d) - 1)
        # If the ticker has fewer than _PRIMING_DAILY_BARS daily bars
        # in total, there's not enough history to produce meaningful
        # signals. Skip.
        if prime_idx >= len(bars_1d) - 1:
            logger.info(
                "signal_backtest: too little history to prime",
                extra={
                    "ticker_id": ticker.ticker_id,
                    "symbol": ticker.ref.symbol,
                    "timeframe": timeframe.value,
                    "available_daily_bars": len(bars_1d),
                    "required_min": _PRIMING_DAILY_BARS + 1,
                },
            )
            return None
        start_date = bars_1d[prime_idx].timestamp.date()

    # --- 3. Choose the bar sequence to replay --- #
    if timeframe == Timeframe.WEEKLY:
        # Synthesize weekly bars from the full daily history. We
        # always feed full history to the aggregator so the weekly
        # buckets are correctly anchored (week N depends only on the
        # daily bars within it; we don't need an "extra priming"
        # adjustment beyond what we already did above, because the
        # priming was about giving the EMA enough WEEKLY bars to
        # settle, which means we need enough DAILY bars to BUILD
        # those weekly bars).
        bars_used = daily_to_weekly(bars_1d, tz=tz)
    else:
        bars_used = bars_1d

    if not bars_used:
        # daily_to_weekly returned nothing (extremely short history).
        return None

    # Belt-and-braces: even if there are NEW daily bars after the
    # resume start_date, the aggregator may bucket them into an
    # already-known weekly anchor (e.g. resume start_date is in
    # the middle of week W, and the only new daily bars are also
    # in week W — daily_to_weekly anchors them to W's Monday,
    # which is BEFORE start_date). In that case we have no genuinely
    # new weekly bar to capture; skip cleanly rather than upsert a
    # through_date < from_date row (CHECK constraint violation).
    recomputed_end_preview = bars_used[-1].timestamp.date()
    if recomputed_end_preview < start_date:
        logger.debug(
            "signal_backtest: new daily bars fall inside an already-"
            "computed weekly bucket — skipping",
            extra={
                "ticker_id": ticker.ticker_id,
                "symbol": ticker.ref.symbol,
                "timeframe": timeframe.value,
                "resume_start": start_date.isoformat(),
                "last_bucket_anchor": recomputed_end_preview.isoformat(),
            },
        )
        return None

    # --- 4. Replay --- #
    # Build a fresh engine. We don't use TickerEngine.prime() because
    # we want to inspect every bar's state to detect ChangeSignals;
    # ingest()-in-a-loop is equivalent and gives us state per bar.
    engine = TickerEngine(ticker.ref, timeframe, params)
    # Track previous display_bg outside the engine — the engine
    # doesn't expose change_signal directly (per Finding 1 in the
    # pre-implementation push-back). Initial value matches the
    # engine's internal _prev_display: NEUTRAL.
    prev_display: CoreRegime = CoreRegime.NEUTRAL

    emitted_events: list[SignalEvent] = []
    computed_at = datetime.now(timezone.utc)

    for bar in bars_used:
        state = engine.ingest(bar)
        signal = classify_change(prev_display, state.display_bg)
        prev_display = state.display_bg

        if signal == ChangeSignal.NO_CHANGE:
            continue

        # Filter: only capture signals on bars AT OR AFTER start_date.
        # Priming bars are silently consumed.
        if bar.timestamp.date() < start_date:
            continue

        emitted_events.append(SignalEvent(
            ticker=ticker.ref,
            timeframe=timeframe,
            bar_timestamp=bar.timestamp,
            signal=signal,
            price_at_signal=bar.close,
            computed_at=computed_at,
        ))
        emitted_counts[signal.name] = emitted_counts.get(signal.name, 0) + 1

    # --- 5. Merge into the in-memory JSON dict --- #
    # The recomputed window's end is the latest bar's date that we
    # actually processed in this run. For weekly, that's the last
    # weekly bucket's anchor (Monday) date.
    recomputed_end = bars_used[-1].timestamp.date()
    deleted = _merge_signals_into(
        in_memory,
        ticker_symbol=ticker.ref.symbol,
        timeframe=timeframe,
        recomputed_window=(start_date, recomputed_end),
        new_events=emitted_events,
    )

    # --- 6. Per-ticker commit of the resume row --- #
    # `computed_from_date` semantics: on a fresh insert, this is the
    # start_date we just computed from. On an update, the repo's
    # UPSERT preserves the original from-date — see the repo's
    # upsert docstring.
    with db_engine.begin() as conn:
        SignalBacktestRunsRepository(conn).upsert(
            exchange=exchange,
            ticker_id=ticker.ticker_id,
            timeframe=timeframe,
            computed_from_date=start_date,
            computed_through_date=recomputed_end,
            last_run_id=run_id,
        )

    return _RecomputedWindow(
        start_date=start_date,
        end_date=recomputed_end,
        deleted_in_merge=deleted,
    )


# ---------- Merge protocol ---------- #


def _merge_signals_into(
    in_memory: dict,
    *,
    ticker_symbol: str,
    timeframe: Timeframe,
    recomputed_window: tuple[date, date],
    new_events: list[SignalEvent],
) -> int:
    """Merge the new events into the in-memory by_date dict per the
    protocol in design §4.2.3.

    For each date in `recomputed_window`:
      - Remove any existing record matching (ticker, timeframe).
      - If a new event exists for that date, insert it.
    For dates outside the window: untouched.
    For new events on dates not yet in the in-memory dict: added.

    Returns the number of prior records that were removed by the merge.
    """
    start, end = recomputed_window
    by_date: dict = in_memory.setdefault("by_date", {})

    # Lookup of new events by ISO date.
    new_by_date: dict[str, dict] = {}
    for ev in new_events:
        d_str = ev.bar_timestamp.date().isoformat()
        # If multiple signals fired on the same date (shouldn't happen
        # for one (ticker, timeframe), but defensive), the last one
        # wins. The engine doesn't emit more than one change per bar
        # because the regime can only transition once per ingest.
        new_by_date[d_str] = _signal_event_to_record(ev)

    deleted = 0
    tf_value = timeframe.value
    # Walk the in-memory by_date entries that fall in the window.
    # Materialize the keys first so we can mutate by_date during the loop.
    for date_str in list(by_date.keys()):
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            # Malformed key — leave it alone. Shouldn't happen for
            # files this code wrote, but defensive against external
            # edits.
            continue
        if not (start <= d <= end):
            continue
        signals_list = by_date[date_str]
        # Filter out existing (ticker, timeframe) entries — those are
        # what we're recomputing.
        kept: list[dict] = []
        for s in signals_list:
            if s.get("ticker") == ticker_symbol and s.get("timeframe") == tf_value:
                deleted += 1
                continue
            kept.append(s)

        # If the engine emitted a NEW signal for this date, add it.
        if date_str in new_by_date:
            kept.append(new_by_date.pop(date_str))

        if kept:
            by_date[date_str] = kept
        else:
            # Empty after removal AND no new signal — drop the date
            # entirely to keep the file tidy.
            del by_date[date_str]

    # Any new_by_date entries left over are for dates not previously
    # in by_date — add them fresh.
    for date_str, rec in new_by_date.items():
        by_date.setdefault(date_str, []).append(rec)

    # Stable sort within each touched date: by (ticker, timeframe).
    # This keeps diffs across runs minimal.
    for date_str in {ev.bar_timestamp.date().isoformat() for ev in new_events}:
        if date_str in by_date:
            by_date[date_str].sort(
                key=lambda s: (s.get("ticker", ""), s.get("timeframe", "")),
            )

    return deleted


def _signal_event_to_record(ev: SignalEvent) -> dict:
    """Project a SignalEvent into the JSON-serializable record shape
    defined by design §4.3."""
    return {
        "ticker": ev.ticker.symbol,
        "timeframe": ev.timeframe.value,
        "signal": ev.signal.name,   # "BUY" / "SELL" / "EXIT_FROM_BUY" / "EXIT_FROM_SELL"
        "price_at_signal": ev.price_at_signal,
        "bar_timestamp": ev.bar_timestamp.isoformat(),
    }


# ---------- JSON read / write ---------- #


def _load_existing_or_empty(path: Path, exchange: str) -> dict:
    """Load the per-exchange JSON file into memory, or return a
    blank skeleton if missing.

    A missing file is the normal case for a first-ever run, and also
    the recommended workflow when the operator wants a guaranteed-
    clean slate (delete the file, then re-run force-full).

    A file that exists but is corrupt or has the wrong exchange is
    treated as a hard error — silently overwriting a different
    exchange's file would lose data."""
    if not path.exists():
        return _empty_skeleton(exchange)
    try:
        with path.open("r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Existing signal file {path} is not valid JSON ({e}). "
            f"Delete it and re-run with force_full to start clean."
        ) from e
    file_exchange = data.get("exchange")
    if file_exchange and file_exchange != exchange:
        raise RuntimeError(
            f"Existing signal file {path} has exchange={file_exchange!r} "
            f"but this run is for exchange={exchange!r}. Refusing to "
            f"overwrite. Did the file path get mixed up?"
        )
    # Ensure the by_date key exists; older / partial files might lack it.
    data.setdefault("by_date", {})
    return data


def _empty_skeleton(exchange: str) -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "exchange": exchange,
        "generated_at": None,
        "generated_by_run_id": None,
        "params": {},
        "counts": {},
        "by_date": {},
    }


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write `data` as JSON to `path` atomically.

    Mechanics: write to `path.tmp` first, flush + fsync, then
    `os.replace` (POSIX rename — atomic). A reader opening `path`
    mid-write sees either the old version or the new version,
    never a half-written one.

    The fsync is on the FILE (durability of the new bytes). We
    don't fsync the containing directory here — that's a tier of
    durability needed for survival of an OS-level crash mid-write,
    not relevant to the merge protocol's correctness invariants.
    Add a directory fsync if backtest durability survives an OS
    crash becomes a requirement.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w") as f:
        json.dump(data, f, separators=(",", ":"), default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _refresh_counts(in_memory: dict) -> None:
    """Recompute the `counts` block from the current `by_date`
    contents. Called at the end of a run; the JSON is the source of
    truth so we count fresh rather than maintaining a running tally
    that could drift."""
    by_date: dict = in_memory.get("by_date", {})
    if not by_date:
        in_memory["counts"] = {
            "tickers": 0,
            "signals_total": 0,
            "signals_by_type": {
                "BUY": 0, "SELL": 0,
                "EXIT_FROM_BUY": 0, "EXIT_FROM_SELL": 0,
            },
            "first_date": None,
            "last_date": None,
        }
        return

    tickers: set[str] = set()
    by_type: dict[str, int] = {
        "BUY": 0, "SELL": 0,
        "EXIT_FROM_BUY": 0, "EXIT_FROM_SELL": 0,
    }
    total = 0
    for _date_str, records in by_date.items():
        for r in records:
            tickers.add(r.get("ticker", ""))
            sig = r.get("signal")
            if sig in by_type:
                by_type[sig] += 1
            total += 1
    # Drop the placeholder empty string if any record was malformed.
    tickers.discard("")
    dates_sorted = sorted(by_date.keys())
    in_memory["counts"] = {
        "tickers": len(tickers),
        "signals_total": total,
        "signals_by_type": by_type,
        "first_date": dates_sorted[0] if dates_sorted else None,
        "last_date": dates_sorted[-1] if dates_sorted else None,
    }


# ---------- Misc helpers ---------- #


def _params_to_dict(p: IndicatorParams) -> dict:
    """Project IndicatorParams into the on-disk params shape (§4.3).
    Mirrors the per-stage nesting of the YAML config."""
    return {
        "ema": {
            "med": {
                "base": p.med_base, "s1": p.med_s1,
                "s2": p.med_s2, "final": p.med_final,
            },
            "long": {
                "base": p.long_base, "s1": p.long_s1,
                "s2": p.long_s2, "final": p.long_final,
            },
            "longest": {
                "base": p.longest_base, "s1": p.longest_s1,
                "s2": p.longest_s2, "final": p.longest_final,
            },
            "volume_sma_length": p.volume_sma_length,
        },
        "atr": {
            "length": p.atr_length,
            "inner_mult": p.inner_mult,
            "mid_mult": p.mid_mult,
            "outer_mult": p.outer_mult,
        },
    }


def _exchange_tz(config: Config, exchange: str) -> str:
    """Look up the exchange's IANA tz string from config.market_hours.
    The exchange has already been validated by `validate_exchange`
    at this point, so KeyError-style failure here would be a bug.
    """
    for mh in config.market_hours:
        if mh.exchange == exchange:
            return mh.tz
    raise RuntimeError(  # pragma: no cover — validate_exchange catches first
        f"BUG: exchange {exchange!r} passed validation but is not in "
        f"market_hours. Inspect validate_exchange / config loading."
    )


def _fetch_active_tickers(db_engine: Engine, exchange: str) -> list[_ActiveTicker]:
    """Active, non-delisted tickers for `exchange`, ordered by
    (symbol ASC). We don't use tier ordering here because
    backtest doesn't have a partial-run priority story — every
    ticker is equally important to the backtester."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT ticker_id, exchange, symbol, instrument
                FROM tickers
                WHERE is_active = TRUE
                  AND delisted_at IS NULL
                  AND exchange = :ex
                ORDER BY symbol ASC
            """),
            {"ex": exchange},
        )
        return [
            _ActiveTicker(
                ticker_id=r.ticker_id,
                ref=TickerRef(
                    exchange=r.exchange,
                    symbol=r.symbol,
                    instrument=r.instrument,
                ),
            )
            for r in rows
        ]


def _one_day():
    """Tiny wrapper so the date+1 step is grep-able and the import
    list stays minimal."""
    from datetime import timedelta
    return timedelta(days=1)
