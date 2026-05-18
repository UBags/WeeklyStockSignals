"""ComputeScheduler — the periodic compute loop.

Responsibilities:
  - On a wall-clock cadence (default hourly), iterate every (ticker, tf)
    in the warm registry.
  - For each: fetch new bars from the DB since the engine's last bar,
    distinguish provisional (still-open bucket) from final, ingest
    accordingly, classify any signal change against the *previous
    compute's* display state (read from the cache), persist a SignalEvent
    if changed, replace the cache entry.
  - Detect historical revisions (bars within the engine's history window
    whose updated_at moved forward). On detection, force a full re-prime
    for that ticker.
  - Be the SINGLE WRITER to the SignalCache. Nothing else writes.
  - Protect against concurrent run_once() calls (push from /admin/compute-now
    overlapping with poll from the daemon thread) via a process-level lock.

The class exposes `run_once()` as the public unit of work, plus
`start()`/`stop()` for the daemon-thread mode. Tests and admin endpoints
should drive `run_once()` directly; production startup calls `start()`.

Thread model:
  start() spawns a daemon thread that loops:
      sleep until next wall-clock tick
      run_once()
  stop() flips a flag and joins the thread.

  run_once() is reentrancy-protected by `_run_lock` — if a second caller
  attempts to start a pass while one is in flight, the second call
  returns a "skipped" summary rather than waiting (avoids HTTP request
  timeouts from /admin/compute-now during a long poll pass).
"""

from __future__ import annotations

import logging
import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Engine, and_, func as sql_func, select

from ..cache.signal_cache import SignalCache, build_cached_signal
from ..domain import (
    ChangeSignal,
    OHLCVBar,
    SignalEvent,
    Timeframe,
)
from ..engine.change_classifier import classify_change
from ..indicators.aggregation import (
    _monday_of,
    daily_to_weekly,
    detect_weekly_gaps,
    expected_closed_weekly_buckets,
    is_provisional,
)
from ..observability import correlation_id, new_correlation_id
from ..observability import metrics as M
from ..observability.tracing import (
    add_correlation_id_to_span,
    get_tracer,
    set_span_attribute_if_active,
)
from ..persistence import (
    BarsRepository,
    SignalsRepository,
    transaction,
)
from ..persistence.tables import ohlcv_bars
from ..registry.ticker_registry import TickerRegistry

logger = logging.getLogger(__name__)


@dataclass
class ComputeRunSummary:
    """Result of one run_once() pass — useful for logs, metrics, and tests."""
    started_at: datetime
    finished_at: datetime | None = None
    tickers_examined: int = 0
    tickers_with_new_bars: int = 0
    bars_ingested: int = 0
    tickers_reprimed: int = 0
    events_fired: list[SignalEvent] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    # (ticker_str, error_message); errors are caught per ticker so one
    # bad ticker doesn't break the whole pass.

    skipped_due_to_concurrent_run: bool = False
    """True if this run_once() invocation was suppressed because another
    run_once() was already in flight."""

    tickers_with_gaps: list[tuple[str, list[str]]] = field(default_factory=list)
    """A2 (Phase 9): tickers whose ETL has missing closed weekly buckets.
    Each entry is (ticker_str, [iso-formatted missing-week timestamps]).
    These tickers were SKIPPED for ingest in this pass — their cache
    entry's `stale_since` is set (or kept). The ETL is expected to fill
    the gap; the next compute pass that finds no gap will clear the
    stale flag."""

    tickers_recovered_from_gap: int = 0
    """A2: count of tickers whose `stale_since` was cleared on this pass
    because the gap was filled."""


class ComputeScheduler:
    """The signal-computation thread."""

    def __init__(
        self,
        *,
        db_engine: Engine,
        registry: TickerRegistry,
        cache: SignalCache,
        cadence_minutes: int = 60,
    ):
        if cadence_minutes < 1:
            raise ValueError("cadence_minutes must be >= 1")
        self._db_engine = db_engine
        self._registry = registry
        self._cache = cache
        self._cadence = timedelta(minutes=cadence_minutes)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Reentrancy protection — push (admin endpoint) and poll (daemon
        # thread) call run_once() independently; this lock ensures at most
        # one pass runs at a time.
        self._run_lock = threading.Lock()

        # Track the latest known updated_at per ticker so we can detect
        # historical revisions on the next pass. Keyed by (ref, tf).
        # Populated lazily on first pass.
        self._last_revision_high_water: dict[tuple, datetime] = {}

        # Track the timestamp of the last successful completion. Used by
        # the /readyz health check to detect a wedged thread.
        self.last_run_completed_at: datetime | None = None

        # True if start() was ever called. Lets /readyz distinguish
        # "never started" (fine in tests) from "started but died" (bad).
        self._was_started: bool = False

        # A6: track when we last extended notification_log partitions.
        # The check runs at most once per UTC day inside run_once().
        self._last_partition_check_date: date | None = None

    # ---------- Single unit of work ---------- #

    def run_once(self) -> ComputeRunSummary:
        """Iterate the registry, ingest any new bars, fire events.

        Errors are caught and recorded per (ticker, timeframe); a failure
        on one ticker doesn't stop the others. Returns the summary.

        Reentrancy-protected: if another run_once() is already in flight,
        returns immediately with `skipped_due_to_concurrent_run=True`.
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
                with tracer.start_as_current_span("compute_scheduler.run_once"):
                    add_correlation_id_to_span()
                    with M.scheduler_run_duration_seconds.time():
                        summary = self._run_once_locked(now)
                    set_span_attribute_if_active(
                        "tickers.examined", summary.tickers_examined,
                    )
                    set_span_attribute_if_active(
                        "bars.ingested", summary.bars_ingested,
                    )
                    set_span_attribute_if_active(
                        "events.fired", len(summary.events_fired),
                    )
                M.scheduler_runs_total.labels(outcome="completed").inc()
                return summary
        finally:
            self._run_lock.release()

    def _run_once_locked(self, now: datetime) -> ComputeRunSummary:
        summary = ComputeRunSummary(started_at=now)

        # A6: extend notification_log partitions forward, at most once
        # per UTC day. Calls the PL/pgSQL function from migration 004
        # for `now + 2 months` so there's always a 2-month forward
        # buffer of partitions. The function is idempotent (uses
        # CREATE TABLE IF NOT EXISTS).
        self._maybe_extend_partitions(now)

        for (ref, tf), engine in self._registry.all_engines():
            summary.tickers_examined += 1
            M.scheduler_tickers_examined_total.inc()
            # Per-ticker correlation ID nested under the run's. The
            # outer run_cid is the active context here; we replace it
            # for the duration of this ticker so each ticker's logs
            # carry a distinct ID and downstream signal events get
            # the same one via current_correlation_id().
            ticker_cid = new_correlation_id()
            with correlation_id(ticker_cid):
                tracer = get_tracer("trip_ema.scheduler")
                with tracer.start_as_current_span(
                    "compute_scheduler.process_ticker",
                ) as span:
                    add_correlation_id_to_span()
                    span.set_attribute("ticker", str(ref))
                    span.set_attribute("timeframe", tf.value)
                    try:
                        events = self._process_ticker(ref, tf, engine, now, summary)
                        summary.events_fired.extend(events)
                        for ev in events:
                            M.scheduler_signal_events_total.labels(
                                signal=ev.signal.name,
                            ).inc()
                        if events:
                            span.set_attribute("events_fired", len(events))
                    except Exception as e:
                        logger.exception(
                            "Compute error",
                            extra={
                                "ticker": str(ref),
                                "timeframe": tf.value,
                            },
                        )
                        summary.errors.append((f"{ref}@{tf.value}", str(e)))
                        M.scheduler_errors_total.inc()
                        span.set_attribute("error", True)
                        span.record_exception(e)

        summary.finished_at = datetime.now(timezone.utc)
        self.last_run_completed_at = summary.finished_at

        # Update gauges that reflect end-of-pass state
        M.scheduler_stale_tickers.set(len(self._cache.stale_signals()))
        M.cache_size.set(len(self._cache))
        M.scheduler_bars_ingested_total.inc(summary.bars_ingested)
        M.scheduler_tickers_reprimed_total.inc(summary.tickers_reprimed)
        logger.info(
            "Compute run: %d tickers, %d with new bars, %d reprimed, "
            "%d events fired, %d errors",
            summary.tickers_examined,
            summary.tickers_with_new_bars,
            summary.tickers_reprimed,
            len(summary.events_fired),
            len(summary.errors),
        )
        return summary

    def _process_ticker(
        self,
        ref,
        tf: Timeframe,
        engine,
        now: datetime,
        summary: ComputeRunSummary,
    ) -> list[SignalEvent]:
        """Ingest new bars for one ticker, detect change, persist & cache.

        Returns the list of SignalEvents that were fired (zero or one
        per run — at most one change is recorded per compute pass).

        B4: When the ticker's source_timeframe differs from the engine's
        target timeframe, this method fetches source bars and aggregates
        before ingestion. The week-anchor for aggregation uses the
        ticker's `tz` (Asia/Kolkata, America/New_York, etc.) so the
        local-Monday convention is honored.
        """
        ticker_id = self._registry.get_ticker_id(ref)
        record = self._registry.get_ticker_meta(ref)
        if record is None:
            # Defensive — should always be populated after prime_all
            logger.warning("No ticker metadata for %s; skipping", ref)
            return []
        source_tf = Timeframe(record.source_timeframe)
        ticker_tz = record.tz
        key = (ref, tf)

        # 1. Detect historical revisions BEFORE ingesting new bars.
        # Revision detection uses the SOURCE timeframe — that's what
        # the ETL writes; an updated source bar is what would invalidate
        # the engine's cached aggregations.
        last_ts = engine.last_bar_timestamp()
        if last_ts is not None:
            history = engine.state_history()
            history_start = history[0].timestamp if history else last_ts
            with self._db_engine.connect() as conn:
                max_updated = BarsRepository(conn).max_updated_at_since(
                    ticker_id, source_tf,
                    history_start_inclusive=history_start,
                )
            prior_high_water = self._last_revision_high_water.get(key)
            if (
                max_updated is not None
                and prior_high_water is not None
                and max_updated > prior_high_water
            ):
                if self._revision_is_historical(
                    ticker_id, source_tf, last_ts, prior_high_water,
                ):
                    logger.info(
                        "Historical revision detected for %s@%s; re-priming",
                        ref, tf.value,
                    )
                    self._registry.reprime(ref, tf, self._db_engine)
                    new_engine = self._registry.get(ref, tf)
                    if new_engine is not None:
                        engine = new_engine
                    summary.tickers_reprimed += 1
                    last_ts = engine.last_bar_timestamp()
            if max_updated is not None:
                self._last_revision_high_water[key] = max_updated

        if last_ts is None:
            logger.debug("Engine for %s has no state; skipping", ref)
            return []

        # 1b. A2 (Phase 9): Gap detection.
        # Before fetching for ingest, verify that no closed weekly buckets
        # between `last_ts` and `now` are missing source bars. If any are,
        # the ETL hasn't backfilled them yet; we refuse to advance the
        # engine and mark the ticker stale until the gap is filled.
        gap_check = self._check_for_data_gap(
            ticker_id, source_tf, ticker_tz, last_ts, now,
        )
        if gap_check is not None:
            missing_weeks = gap_check
            logger.warning(
                "Data gap detected for %s@%s: missing %d closed weekly bucket(s) %s. "
                "Refusing to advance engine; awaiting ETL backfill.",
                ref, tf.value, len(missing_weeks),
                [m.isoformat() for m in missing_weeks],
            )
            self._mark_ticker_stale(ref, tf, now, missing_weeks, summary)
            return []
        # No gap. If we had previously marked stale, clear the flag.
        self._clear_stale_if_set(ref, tf, now, summary)

        # 2. Fetch new bars and build the candidate target-tf bars to
        # ingest. Branches by source-vs-target relationship.
        new_bars = self._fetch_new_bars_for_ingest(
            ticker_id, tf, source_tf, ticker_tz, last_ts, engine, now,
        )
        if not new_bars:
            return []

        summary.tickers_with_new_bars += 1
        summary.bars_ingested += len(new_bars)

        # 3. Remember the display state from the cached snapshot.
        previous_cached = self._cache.get(ref, tf)
        previous_display = (
            previous_cached.latest_state.display_bg
            if previous_cached is not None
            else engine.latest_state().display_bg
        )

        # 4. Ingest. The LAST bar may be provisional (its bucket is open
        # in the ticker's tz). Earlier bars are always final.
        for i, bar in enumerate(new_bars):
            is_last = (i == len(new_bars) - 1)
            if is_last and is_provisional(bar.timestamp, tf, now, tz=ticker_tz):
                engine.ingest_provisional(bar)
            else:
                engine.ingest(bar)

        latest = engine.latest_state()
        assert latest is not None

        # 5. Classify the change.
        change = classify_change(previous_display, latest.display_bg)

        # 6. Persist the event if changed. (No ticker_signal_state upsert.)
        fired_events: list[SignalEvent] = []
        if change != ChangeSignal.NO_CHANGE:
            event = SignalEvent(
                ticker=ref,
                timeframe=tf,
                bar_timestamp=latest.timestamp,
                signal=change,
                price_at_signal=engine.last_close(),
                computed_at=now,
            )
            with transaction(self._db_engine) as conn:
                SignalsRepository(conn).insert_event(ticker_id, event)
            fired_events.append(event)

        # 7. Build the new cached snapshot.
        if change != ChangeSignal.NO_CHANGE:
            last_change = change
            last_change_bar = latest.timestamp
            last_change_computed_at = now
        elif previous_cached is not None:
            last_change = previous_cached.last_change
            last_change_bar = previous_cached.last_change_bar
            last_change_computed_at = previous_cached.last_change_computed_at
        else:
            last_change = ChangeSignal.NO_CHANGE
            last_change_bar = None
            last_change_computed_at = None

        self._cache.put(build_cached_signal(
            ticker=ref,
            timeframe=tf,
            latest_state=latest,
            latest_close=engine.last_close(),
            state_history=engine.state_history(),
            last_change=last_change,
            last_change_bar=last_change_bar,
            last_change_computed_at=last_change_computed_at,
            computed_at=now,
        ))

        return fired_events

    def _fetch_new_bars_for_ingest(
        self,
        ticker_id: int,
        engine_tf: Timeframe,
        source_tf: Timeframe,
        ticker_tz: str,
        engine_last_ts: datetime,
        engine,
        now: datetime,
    ) -> list[OHLCVBar]:
        """Fetch new bars at the source resolution, aggregate to the
        engine's target timeframe if needed, return the list to ingest.

        Three cases:

        1. source == target. Direct fetch_since on the target timeframe.
           Behaves identically to pre-B4.

        2. source finer than target, target == WEEKLY. Fetch all source
           bars from the current local-week's start onwards (the
           current week's provisional bar will be re-aggregated from
           scratch on every compute pass — clean and idempotent).
           Aggregate to one provisional weekly bar via
           daily_to_weekly(..., tz=ticker_tz).

        3. Other combinations. NotImplementedError.

        Why "from current week's start" in case 2: the engine already
        has the prior weekly bar committed (or held as provisional,
        which will be committed-then-replaced via the A1 erased-bar fix
        if a new week's bars arrive). Re-fetching all source bars from
        Monday means: if 17 hourly bars have rolled in this week, we
        rebuild the week's aggregated bar from those 17 each cycle. The
        engine's ingest_provisional handles the rollback automatically.
        """
        if source_tf == engine_tf:
            # Case 1: direct path. Same logic as pre-B4.
            with self._db_engine.connect() as conn:
                bars_repo = BarsRepository(conn)
                if engine.has_provisional():
                    return bars_repo.fetch_since(
                        ticker_id, engine_tf,
                        engine_last_ts - timedelta(microseconds=1),
                    )
                return bars_repo.fetch_since(
                    ticker_id, engine_tf, engine_last_ts,
                )

        if engine_tf == Timeframe.WEEKLY:
            # Case 2: source-aggregate path. Fetch all source bars from
            # the current local-week's start, aggregate to one weekly.
            #
            # We use _monday_of(now, tz=ticker_tz) to find the start of
            # the *current* local week. fetch_since is strictly
            # exclusive, so we subtract one microsecond to make it
            # inclusive of the Monday-midnight bar (which, in practice,
            # might not exist exactly, but this is the safest bound).
            current_week_start = _monday_of(now, tz=ticker_tz)
            with self._db_engine.connect() as conn:
                bars_repo = BarsRepository(conn)
                source_bars = bars_repo.fetch_since(
                    ticker_id, source_tf,
                    current_week_start - timedelta(microseconds=1),
                )
            if not source_bars:
                return []
            # Aggregate to weekly. With include_partial_last_week=True
            # and source bars all from the current week, we get exactly
            # one weekly bar: the current provisional.
            weekly_bars = daily_to_weekly(
                source_bars,
                include_partial_last_week=True,
                tz=ticker_tz,
            )
            # Filter: only include weekly bars that are NEWER than
            # the engine's last_ts, OR equal to it (the provisional
            # bar gets re-applied via ingest_provisional's rollback
            # path; the equal-timestamp case is the in-week update).
            return [
                wb for wb in weekly_bars
                if wb.timestamp >= engine_last_ts
            ]

        raise NotImplementedError(
            f"Scheduler can't handle source={source_tf.value} → "
            f"target={engine_tf.value}"
        )

    # ---------- A2: gap detection & stale tracking ---------- #

    def _check_for_data_gap(
        self,
        ticker_id: int,
        source_tf: Timeframe,
        ticker_tz: str,
        engine_last_ts: datetime,
        now: datetime,
    ) -> list[datetime] | None:
        """Check whether any expected closed weekly bucket between
        `engine_last_ts` and `now` is missing source bars.

        Returns:
            None if there's no gap (or if no closed buckets are expected,
            e.g., engine is up to date).
            A non-empty list of missing weekly Monday timestamps (UTC)
            if a gap is detected.

        Implementation: queries the DB for the set of local-Mondays
        that have at least one bar in the window, then computes the
        set difference against the expected closed buckets. This is
        O(non-empty weeks) over the wire rather than O(all bars in
        the window) — important when the source is hourly or when an
        outage spans many weeks.

        Only applies when the engine's target is WEEKLY. For other
        timeframes (not currently supported by the system), returns None
        — gap detection isn't defined for them yet.
        """
        expected = expected_closed_weekly_buckets(engine_last_ts, now, tz=ticker_tz)
        if not expected:
            return None
        first_expected = expected[0]
        with self._db_engine.connect() as conn:
            bars_repo = BarsRepository(conn)
            present_mondays = bars_repo.weekly_buckets_with_bars(
                ticker_id=ticker_id,
                timeframe=source_tf,
                since_inclusive=first_expected,
                tz=ticker_tz,
            )
        gaps = [m for m in expected if m not in present_mondays]
        return gaps if gaps else None

    def _mark_ticker_stale(
        self,
        ref,
        tf: Timeframe,
        now: datetime,
        missing_weeks: list[datetime],
        summary: ComputeRunSummary,
    ) -> None:
        """Set `stale_since` on the cache entry. Preserves all other
        cache fields — `latest_state`, `last_change`, etc. — so API
        callers can still read the last-known-good signal alongside
        the stale indication.

        If the entry was already stale (stale_since was set on a prior
        pass), preserve the original stale_since timestamp — that's the
        moment the gap was *first* observed, which is more useful for
        ops than "the most recent pass that re-confirmed staleness."
        """
        existing = self._cache.get(ref, tf)
        if existing is None:
            # Defensive: a brand-new ticker (no cache entry) with a gap
            # would land here. We can't write a CachedSignal without a
            # latest_state. Skip the cache write but still record in
            # the summary.
            summary.tickers_with_gaps.append(
                (str(ref) + "@" + tf.value, [m.isoformat() for m in missing_weeks])
            )
            return
        stale_since = existing.stale_since if existing.stale_since else now
        replacement = build_cached_signal(
            ticker=existing.ticker,
            timeframe=existing.timeframe,
            latest_state=existing.latest_state,
            latest_close=existing.latest_close,
            state_history=list(existing.state_history),
            last_change=existing.last_change,
            last_change_bar=existing.last_change_bar,
            last_change_computed_at=existing.last_change_computed_at,
            computed_at=existing.computed_at,   # NOT now — preserve last good compute time
            stale_since=stale_since,
        )
        self._cache.put(replacement)
        summary.tickers_with_gaps.append(
            (str(ref) + "@" + tf.value, [m.isoformat() for m in missing_weeks])
        )

    def _clear_stale_if_set(
        self,
        ref,
        tf: Timeframe,
        now: datetime,
        summary: ComputeRunSummary,
    ) -> None:
        """If the cache entry's stale_since is set, clear it. Called on
        any pass that proceeds normally (no gap detected). Counts the
        recovery in the summary for ops visibility."""
        existing = self._cache.get(ref, tf)
        if existing is None or existing.stale_since is None:
            return
        recovered = build_cached_signal(
            ticker=existing.ticker,
            timeframe=existing.timeframe,
            latest_state=existing.latest_state,
            latest_close=existing.latest_close,
            state_history=list(existing.state_history),
            last_change=existing.last_change,
            last_change_bar=existing.last_change_bar,
            last_change_computed_at=existing.last_change_computed_at,
            computed_at=existing.computed_at,
            stale_since=None,
        )
        self._cache.put(recovered)
        summary.tickers_recovered_from_gap += 1
        logger.info(
            "Cleared stale flag for %s@%s — gap filled",
            ref, tf.value,
        )

    def _revision_is_historical(
        self,
        ticker_id: int,
        source_tf: Timeframe,
        engine_last_ts: datetime,
        prior_high_water: datetime,
    ) -> bool:
        """Check whether any source bar OLDER than the engine's last_ts
        was recently updated. If yes, we have a historical revision and
        need to re-prime.

        Note `source_tf` is intentionally the *source* timeframe (what the
        ETL writes), not the engine's target. A late-arriving or revised
        source bar is what would invalidate the engine's cached
        aggregations — querying at the target timeframe would miss those.
        """
        with self._db_engine.connect() as conn:
            stmt = (
                select(sql_func.count())
                .select_from(ohlcv_bars)
                .where(and_(
                    ohlcv_bars.c.ticker_id == ticker_id,
                    ohlcv_bars.c.timeframe == source_tf.value,
                    ohlcv_bars.c.bar_timestamp < engine_last_ts,
                    ohlcv_bars.c.updated_at > prior_high_water,
                ))
            )
            count = conn.execute(stmt).scalar() or 0
        return count > 0

    def _maybe_extend_partitions(self, now: datetime) -> None:
        """A6: keep notification_log partitions extended forward.

        Called at the start of each run_once(). Skips the DB call if
        already done today (UTC). Calls
        `ensure_notification_log_partition(NOW() + INTERVAL '2 months')`
        — the PL/pgSQL function is idempotent (CREATE TABLE IF NOT
        EXISTS), so repeating it across multiple instances is safe.

        Without this, migration 004's 2-month forward buffer would
        eventually exhaust and the notifier worker's INSERT into
        notification_log would crash with
        `no partition of relation "notification_log"`.
        """
        today_utc = now.date()
        if self._last_partition_check_date == today_utc:
            return
        try:
            from sqlalchemy import text
            with self._db_engine.begin() as conn:
                # Extend two months forward.
                conn.execute(text(
                    "SELECT ensure_notification_log_partition("
                    "    (NOW() + INTERVAL '2 months')::DATE"
                    ")"
                ))
            self._last_partition_check_date = today_utc
            logger.info(
                "Partition extension call succeeded for %s+2 months",
                today_utc.isoformat(),
            )
        except Exception:
            logger.exception(
                "Partition extension failed; will retry tomorrow"
            )
            # We do NOT update _last_partition_check_date on failure —
            # this means we'll retry on the next run_once() call, which
            # is the right behavior for a transient DB issue. The
            # broader try/except in `run` keeps the scheduler alive.

    # ---------- Daemon thread mode ---------- #

    def start(self) -> None:
        """Spawn the background thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ComputeScheduler", daemon=True,
        )
        self._thread.start()
        self._was_started = True
        logger.info("ComputeScheduler started (cadence=%s)", self._cadence)

    def stop(self, timeout: float | None = 30.0) -> None:
        """Request shutdown; wait up to `timeout` seconds for the thread
        to exit. Idempotent."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("ComputeScheduler stopped")

    def is_alive(self) -> bool:
        """For health-check use: is the daemon thread actually running?"""
        return self._thread is not None and self._thread.is_alive()

    def is_unhealthy(self, now: datetime, stale_after_multiplier: float = 2.0) -> str | None:
        """Return a description of why this scheduler is unhealthy, or
        None if it's fine.

        Healthy means:
          (a) Either start() was never called (test/dev mode), OR the
              thread was started and is still alive.
          (b) If the thread has been started AND has had time to complete
              at least one pass (more than `stale_after_multiplier *
              cadence` has elapsed since start), then last_run_completed_at
              must be within `stale_after_multiplier * cadence` of now.
        """
        if not self._was_started:
            return None
        if not self.is_alive():
            return "ComputeScheduler thread died"
        # Check for wedged thread: if started but never completed, or
        # last completion is too old.
        stale_threshold = self._cadence * stale_after_multiplier
        if self.last_run_completed_at is None:
            # No completion yet. Only a problem if more than threshold
            # has elapsed since boot — but we don't track boot time
            # here; assume "no completion within threshold" by relying
            # on the threshold check below being skipped.
            return None
        gap = now - self.last_run_completed_at
        if gap > stale_threshold:
            return (
                f"ComputeScheduler hasn't completed in {gap.total_seconds():.0f}s "
                f"(threshold: {stale_threshold.total_seconds():.0f}s)"
            )
        return None

    def _loop(self) -> None:
        """Wall-clock-anchored loop. Sleeps until the next cadence
        boundary, then runs."""
        while not self._stop_event.is_set():
            sleep_seconds = _seconds_until_next_tick(
                datetime.now(timezone.utc), self._cadence,
            )
            # Sleep in 1-second chunks so stop() is responsive.
            slept = 0.0
            while slept < sleep_seconds and not self._stop_event.is_set():
                chunk = min(1.0, sleep_seconds - slept)
                time_module.sleep(chunk)
                slept += chunk
            if self._stop_event.is_set():
                break
            try:
                self.run_once()
            except Exception:
                # run_once already handles per-ticker errors; reaching here
                # means an outer-level catastrophe. Log and continue — we
                # do NOT let the loop die.
                logger.exception("Unhandled error in compute loop; continuing")


def _seconds_until_next_tick(now: datetime, cadence: timedelta) -> float:
    """How long to wait until the next cadence boundary, measured from
    the Unix epoch. So with cadence=60min, ticks fire at the top of every
    hour (relative to UTC midnight on Jan 1, 1970).

    Made a free function so it's unit-testable without spinning a thread.
    """
    cadence_seconds = cadence.total_seconds()
    epoch_seconds = now.timestamp()
    # Seconds since the last tick boundary.
    elapsed_in_window = epoch_seconds % cadence_seconds
    remaining = cadence_seconds - elapsed_in_window
    if remaining <= 0:
        remaining = cadence_seconds
    return remaining
