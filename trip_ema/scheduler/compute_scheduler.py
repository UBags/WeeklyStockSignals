"""ComputeScheduler — the periodic compute loop.

# Step 9c rewrite

The OLD scheduler iterated `TickerRegistry.all_engines()` and called
`engine.ingest()` per-bar, then `classify_change` against the prior
display state. After Step 9 demolition, neither the registry nor the
engine exists. The new path:

1. Iterate active tickers via a single DB query (`tickers WHERE
   is_active`).
2. For each, call `signals.generate_live_signal_for_ticker` from
   Step 6. That function loads daily bars, aggregates to weekly,
   computes the unified regime, generates a signal if applicable,
   persists it, and (if a writer is bound) updates the cache.
3. Aggregate per-ticker `LiveTickResult`s into a `ComputeRunSummary`.
4. Mark stale on `skip_reason in {no_bars, insufficient_history}`;
   clear stale on a successful pass that wasn't previously stale.

What's preserved from the OLD scheduler:
  - Daemon-thread `start()` / `stop()` machinery.
  - `_maybe_extend_partitions` — `notification_log` partition
    rollover, at most once per UTC day. Operational concern,
    independent of signal computation.
  - Reentrancy protection via `_run_lock` — push from
    `/admin/compute-now` overlapping with the poll loop returns
    `skipped_due_to_concurrent_run=True` rather than queueing.
  - Per-ticker correlation IDs nested under the run's.
  - Per-ticker error isolation; one bad ticker doesn't kill the pass.

What's dropped from the OLD scheduler:
  - The `_check_for_data_gap` helper that diffed expected vs present
    weekly buckets. Step 6 already returns
    `skip_reason='no_bars'` when the latest expected week is empty,
    which is sufficient signal for stale-flagging. Operators who want
    finer-grained gap diagnostics can read `bar_timestamp` on the
    latest signal_events row directly.
  - Historical revision detection (`_revision_is_historical`). The
    OLD engine held in-memory state that could go stale on a
    historical edit; the new path reloads bars from DB each tick,
    so revisions are picked up automatically.
  - `_discover_source_timeframe`. Step 6 hardcodes daily input (1D)
    and weekly output; sub-daily source timeframes aren't on the
    table for the current product.
"""

from __future__ import annotations

import logging
import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Engine, text

from ..cache.signal_cache import SignalCache
from ..domain import SignalEvent, Timeframe
from ..observability import correlation_id, new_correlation_id
from ..observability import metrics as M
from ..observability.tracing import (
    add_correlation_id_to_span,
    get_tracer,
    set_span_attribute_if_active,
)
from ..persistence.tickers_repo import TickersRepository
from ..signals.generate_signals_in_live_production import (
    LiveTickResult,
    generate_live_signal_for_ticker,
)

logger = logging.getLogger(__name__)


@dataclass
class ComputeRunSummary:
    """Result of one `run_once()` pass — logged, exported as metrics,
    returned via `POST /v1/admin/compute-now`."""
    started_at: datetime
    finished_at: datetime | None = None
    tickers_examined: int = 0
    tickers_with_new_event: int = 0
    """Tickers where Step 6 emitted a SignalEvent on this pass."""
    events_fired: list[SignalEvent] = field(default_factory=list)
    tickers_marked_stale: int = 0
    tickers_recovered_from_gap: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    """(ticker_str, error_message) — per-ticker exceptions during the
    pass. The pass continues past each error."""
    skipped_due_to_concurrent_run: bool = False


class ComputeScheduler:
    """The periodic compute thread."""

    def __init__(
        self,
        *,
        db_engine: Engine,
        cache: SignalCache,
        cadence_minutes: int = 60,
        timeframe: Timeframe = Timeframe.WEEKLY,
    ):
        if cadence_minutes < 1:
            raise ValueError("cadence_minutes must be >= 1")
        self._db_engine = db_engine
        self._cache = cache
        self._cadence = timedelta(minutes=cadence_minutes)
        self._timeframe = timeframe
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Reentrancy protection — push (admin endpoint) and poll
        # (daemon thread) call run_once() independently; this lock
        # ensures at most one pass runs at a time.
        self._run_lock = threading.Lock()

        # Last successful completion. Used by /readyz to detect a
        # wedged thread.
        self.last_run_completed_at: datetime | None = None
        self._was_started: bool = False

        # `notification_log` partition extension — runs at most once
        # per UTC day inside run_once().
        self._last_partition_check_date: date | None = None

    # ---------- Public surface ---------- #

    def run_once(self) -> ComputeRunSummary:
        """Iterate active tickers, call Step 6's live tick path per
        ticker, persist signals and update the cache.

        Reentrancy-protected: if another `run_once()` is already in
        flight, returns immediately with
        `skipped_due_to_concurrent_run=True`.
        """
        now = datetime.now(timezone.utc)
        if not self._run_lock.acquire(blocking=False):
            logger.info("run_once skipped — another pass already in flight")
            M.scheduler_runs_total.labels(outcome="skipped_concurrent").inc()
            return ComputeRunSummary(
                started_at=now,
                finished_at=now,
                skipped_due_to_concurrent_run=True,
            )
        try:
            run_cid = new_correlation_id()
            with correlation_id(run_cid):
                tracer = get_tracer("trip_ema.scheduler")
                with tracer.start_as_current_span(
                    "compute_scheduler.run_once",
                ):
                    add_correlation_id_to_span()
                    with M.scheduler_run_duration_seconds.time():
                        summary = self._run_once_locked(now)
                    set_span_attribute_if_active(
                        "tickers.examined", summary.tickers_examined,
                    )
                    set_span_attribute_if_active(
                        "events.fired", len(summary.events_fired),
                    )
                M.scheduler_runs_total.labels(outcome="completed").inc()
                self.last_run_completed_at = datetime.now(timezone.utc)
                return summary
        finally:
            self._run_lock.release()

    def start(self) -> None:
        """Spawn the daemon thread that loops `run_once()` at the
        configured cadence. Idempotent — calling twice is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="compute_scheduler", daemon=True,
        )
        self._thread.start()
        self._was_started = True
        logger.info("ComputeScheduler started — cadence=%s", self._cadence)

    def stop(self, timeout: float | None = 30.0) -> None:
        """Signal the daemon to exit and join. Idempotent."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "ComputeScheduler did not stop within %.1fs", timeout or -1,
            )
        self._thread = None
        logger.info("ComputeScheduler stopped")

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------- Internals ---------- #

    def _run_once_locked(self, now: datetime) -> ComputeRunSummary:
        summary = ComputeRunSummary(started_at=now)

        # Operational: extend notification_log partitions forward at
        # most once per UTC day. Calls the PL/pgSQL function from
        # migration 0004 idempotently.
        self._maybe_extend_partitions(now)

        # Pull the active-ticker list once at pass start. We don't
        # hold the connection across per-ticker work — that would
        # serialise Step 6's per-ticker connections through this
        # one and tie up a transaction needlessly.
        with self._db_engine.connect() as conn:
            tickers = TickersRepository(conn).find_active()

        for record in tickers:
            summary.tickers_examined += 1
            M.scheduler_tickers_examined_total.inc()
            ticker_cid = new_correlation_id()
            with correlation_id(ticker_cid):
                tracer = get_tracer("trip_ema.scheduler")
                with tracer.start_as_current_span(
                    "compute_scheduler.process_ticker",
                ) as span:
                    add_correlation_id_to_span()
                    span.set_attribute("ticker", str(record.ref))
                    span.set_attribute("timeframe", self._timeframe.value)
                    try:
                        self._process_ticker(record, now, summary)
                    except Exception as e:
                        logger.exception(
                            "Error processing %s@%s",
                            record.ref, self._timeframe.value,
                        )
                        summary.errors.append((str(record.ref), str(e)))

        summary.finished_at = datetime.now(timezone.utc)
        logger.info(
            "Compute pass done: %d examined, %d events, %d stale, "
            "%d recovered, %d errors",
            summary.tickers_examined,
            len(summary.events_fired),
            summary.tickers_marked_stale,
            summary.tickers_recovered_from_gap,
            len(summary.errors),
        )
        return summary

    def _process_ticker(
        self,
        record,
        now: datetime,
        summary: ComputeRunSummary,
    ) -> None:
        """Call Step 6's live tick path for one ticker, update cache
        stale flag based on the result, accumulate the summary.

        Each call opens its own short-lived connection. Step 6 does
        all the work inside the connection: bar load, regime walk,
        signal generation, persistence (in its own transaction),
        and cache write via the bound `update_signal_event` callback.
        """
        with self._db_engine.connect() as conn:
            result: LiveTickResult = generate_live_signal_for_ticker(
                conn,
                ticker_id=record.ticker_id,
                ticker_ref=record.ref,
                timeframe=self._timeframe,
                now=now,
                signal_cache_writer=self._cache.update_signal_event,
            )

        # Stale flagging based on Step 6's skip_reason.
        if result.skip_reason in ("no_bars", "insufficient_history"):
            previously_stale = self._snapshot_was_stale(record.ticker_id)
            self._cache.mark_stale(record.ticker_id, self._timeframe, now)
            if not previously_stale:
                summary.tickers_marked_stale += 1
        else:
            if self._snapshot_was_stale(record.ticker_id):
                self._cache.clear_stale(record.ticker_id, self._timeframe)
                summary.tickers_recovered_from_gap += 1

        if result.signal_event is not None:
            summary.tickers_with_new_event += 1
            summary.events_fired.append(result.signal_event)

    def _snapshot_was_stale(self, ticker_id: int) -> bool:
        snap = self._cache.get_snapshot(ticker_id, self._timeframe)
        return snap is not None and snap.stale_since is not None

    # ---------- Operational ---------- #

    def _maybe_extend_partitions(self, now: datetime) -> None:
        """Once-per-UTC-day call into the PL/pgSQL partition-extension
        function (migration 0004). Idempotent on the DB side; we
        gate at most once per day in-process to avoid log spam."""
        today = now.date()
        if self._last_partition_check_date == today:
            return
        try:
            target = now + timedelta(days=62)
            with self._db_engine.begin() as conn:
                conn.execute(text(
                    "SELECT ensure_notification_log_partition(CAST(:t AS DATE))"
                ), {"t": target.date().isoformat()})
            self._last_partition_check_date = today
            logger.debug(
                "Extended notification_log partition through %s",
                target.date(),
            )
        except Exception:
            # The function may not exist on older DBs; non-fatal.
            logger.exception(
                "ensure_notification_log_partition failed (non-fatal)"
            )

    def _poll_loop(self) -> None:
        """Daemon thread body. Sleep, run, repeat."""
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Unhandled error in compute pass")
            # Sleep in small slices so stop() responds quickly.
            slept = timedelta()
            slice_ = timedelta(seconds=1)
            while slept < self._cadence and not self._stop_event.is_set():
                time_module.sleep(slice_.total_seconds())
                slept += slice_
