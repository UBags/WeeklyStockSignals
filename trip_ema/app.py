"""Main entrypoint for trip_ema.

Boot sequence:
    1. Parse CLI args (config path, dev mode flag)
    2. Configure logging
    3. Load + validate config
    4. Open DB engine
    5. Apply pending migrations
    6. Build SignalCache; prime from DB
    7. Build ComputeScheduler; start its background thread
    8. Build transports + SubscriberDistributor (optional, if subscribers configured)
    9. Wire dependencies into the FastAPI app
    10. Run uvicorn

Graceful shutdown on SIGINT/SIGTERM:
    - Stop SubscriberDistributor
    - Stop ComputeScheduler
    - Dispose DB engine
    - uvicorn exits

Usage:
    python -m trip_ema.app --config path/to/config.yaml
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, TYPE_CHECKING

if TYPE_CHECKING:
    from .transports.http.pulse_state import PulseState

# PHASE-1A-DISPATCHER-BUILD: new batch-run subsystem imports.
from .persistence.update_run_in_progress_repo import (
    UpdateRunInProgressRepository,
)
from .persistence import transaction as _txn_for_phase1a
from .scheduler.run_dispatcher import RunDispatcher
from .scheduler.run_queue import RunQueue

from .cache import SignalCache
from .cache.current_signals_cache import CurrentSignalsCache
from .config import load_config
from .notifications import EmailTransport, NullTransport, WebhookTransport
from .notifications.base import NotificationTransport
from .orchestrator import notify_subscribers, recompute_market
from .persistence import (
    create_engine_from_config,
)
from .persistence.backfill_runs_repo import BackfillRunsRepository
from .persistence.current_signals_repo import CurrentSignalsRepository
# === STEP 14B-E6-import: ComputeScheduler + SubscriberDistributor imports stripped ===
from .scheduler import (
    MarketHoursService,
    OrchestratorPool,
    OrchestratorSettings,
    build_pool_from_configs,
)
from .transports.http import AppState, create_app, set_state

logger = logging.getLogger(__name__)


def _configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Configure structured logging via the observability module.

    json_output=True is the production setting (one JSON object per
    line, suitable for Loki/ELK). json_output=False uses a
    human-friendly text format with correlation_id suffix.
    """
    from .observability import configure_logging as _obs_configure
    _obs_configure(level=level, json_output=json_output)


def _build_app_state(config_path: Path) -> AppState:
    """Steps 3-8: everything except the HTTP server itself.

    Returns a fully-populated AppState ready to be handed to the HTTP layer
    via set_state(). Schedulers are STARTED before this returns — they're
    running in background threads from this point on.
    """
    logger.info("Loading config from %s", config_path)
    config = load_config(config_path)

    # Phase 10: initialize OpenTelemetry tracing if enabled. Safe no-op
    # if disabled; spans throughout the codebase use the no-op tracer.
    if config.observability.tracing.enabled:
        from .observability import configure_tracing
        configure_tracing(
            service_name=config.observability.tracing.service_name,
            exporter=config.observability.tracing.exporter,
            otlp_endpoint=config.observability.tracing.otlp_endpoint,
        )

    logger.info("Connecting to DB: %s", config.database.url_without_password())
    db_engine = create_engine_from_config(config)

    # Phase 12: Alembic is the exclusive migration system. Two cases:
    #
    # (a) Fresh DB — neither schema_migrations nor alembic_version
    #     exists. The bridge is a no-op; `alembic upgrade head` runs
    #     0001_initial_schema.upgrade() which creates the whole
    #     schema (squashed equivalent of legacy 001-010).
    #
    # (b) Pre-Phase-12 DB — schema_migrations is populated through
    #     version 010. The bridge stamps 0001_initial_schema WITHOUT
    #     re-running the DDL (the schema already matches). Then
    #     `alembic upgrade head` applies any post-squash revisions.
    #
    # Both paths converge on `alembic upgrade head`. The legacy
    # `apply_pending` call is no longer invoked from startup; the
    # SQL files in migrations/legacy/ are archived for human
    # reference only.
    from .persistence.alembic_bridge import (
        run_alembic_upgrade_head, stamp_baseline_if_needed,
    )
    try:
        stamp_baseline_if_needed(db_engine, config_path=config_path)
        run_alembic_upgrade_head(config_path=config_path)
    except Exception as e:
        # Alembic failures are fatal — without a working migration
        # path, we can't guarantee the schema matches what the rest
        # of the code expects. The legacy "warn and continue"
        # behavior was a transitional courtesy during Phase 11.
        logger.error("Migration failed: %s", e)
        raise

    logger.info("Building cache")
    cache = SignalCache()

    logger.info("Priming all tickers from DB")
    from trip_ema.api.lifecycle import prime_cache_from_db
    result = prime_cache_from_db(db_engine=db_engine, cache=cache)
    logger.info("Cache primed: %d snapshots", result.snapshots_populated)

    # Step 10 cutover: build the V1 CurrentSignalsCache alongside the
    # legacy SignalCache. Both coexist until Step 14 demolishes the
    # latter. Primed from `current_signals` so GUI reads against the
    # V1 cache work immediately at startup — not only after the first
    # recompute_market cycle.
    logger.info("Building V1 current_signals cache")
    v1_cache = CurrentSignalsCache()
    with db_engine.connect() as conn:
        v1_signals = CurrentSignalsRepository(conn).list_all()
    v1_cache.rebuild_from(v1_signals)
    logger.info(
        "V1 cache primed: %d signals", v1_cache.signal_count(),
    )

    # === STEP 14B-E6-cs_build: ComputeScheduler construction removed ===
    # The orchestrator pool (Phase 14) owns all compute cadence in V1.
    market_hours = MarketHoursService(config.market_hours)

    # === STEP 14B-E6-dist_build: SubscriberDistributor construction removed ===
    # V1 uses notify_subscribers (invoked by the orchestrator pool as
    # distribute_run_once) for the send path. The legacy in-process
    # distributor + Redis-queue distributor are both gone.
    # Redis queue is still built when configured — it now feeds the
    # notifier worker (if anyone runs it) but isn't required for V1.
    redis_queue = None
    if config.redis is not None:
        redis_queue = _build_redis_queue(config.redis)
        logger.info(
            "Redis queue configured at %s (queue=%s)",
            config.redis.url_without_password(), config.redis.queue_name,
        )

    # Phase 10: start Prometheus /metrics server (separate port).
    # Disabled if `config.observability.metrics_port is None`.
    from .observability import start_metrics_server
    from .observability.metrics import set_build_info
    from . import __version__
    set_build_info(__version__)
    metrics_server = start_metrics_server(config.observability.metrics_port)

    # Phase 11 Slice 2: ETL admin endpoints fire long-running runner
    # functions onto this executor so the HTTP request returns
    # immediately. max_workers=2 lets one backfill + one EoD coexist
    # but blocks accidental double-fires.
    from concurrent.futures import ThreadPoolExecutor
    etl_executor = ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="trip-ema-etl-",
    )

    # Phase 14: build the per-market orchestrator pool + pulse state.
    # The pool's threads are NOT started here — they're started during
    # the FastAPI lifespan startup callback, so the asyncio loop is
    # already running when the first orchestrator publishes (PulseState
    # needs the loop for SSE fan-out). When the orchestrator is
    # disabled, both are None and the new endpoints return 503.
    # === STEP 14B-E6-boc_call: _build_orchestrator_components no longer takes scheduler/distributor ===
    pulse_state, orchestrator_pool = _build_orchestrator_components(
        config=config,
        db_engine=db_engine,
        v1_cache=v1_cache,
    )

    # PHASE-1A-BATCH-CALLSITE: build the run-queue + dispatcher
    # (dispatcher thread is not started here — that happens in the
    # lifespan startup callback, same pattern as orchestrator_pool).
    run_queue, run_dispatcher = _build_phase1a_batch_subsystem(
        db_engine=db_engine,
    )

    # === STEP 14B-FIXUP1-app-appstate: v1_cache plumbed through ===
    return AppState(
        config=config,
        db_engine=db_engine,
        cache=cache,
        v1_cache=v1_cache,
        redis_queue=redis_queue,
        metrics_server=metrics_server,
        etl_executor=etl_executor,
        orchestrator_pool=orchestrator_pool,
        pulse_state=pulse_state,
        run_queue=run_queue,             # PHASE-1A-BATCH-CALLSITE
        run_dispatcher=run_dispatcher,   # PHASE-1A-BATCH-CALLSITE
    )


def _is_eod_now_nse() -> bool:
    """Step 13a: is the current wall-clock time PAST NSE close?

    NSE closes at 15:30 IST. The orchestrator's scheduled ticks
    fall at 09:30 / 12:15 / 15:00 / 16:30 IST — only the 16:30
    tick fires after close, so this returns True ONLY for that
    tick. Pre-close ticks → False (intraday); the post-close tick
    → True (EoD).

    Hardcoded for NSE because the V1 cycle is NSE-only today
    (per _make_v1_compute_run_once which also hardcodes
    exchange="NSE"). When BSE/NYSE wiring lands (queued Phase
    14.7+ work), both this helper AND _make_v1_compute_run_once
    get parameterized by exchange together.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    # Tuple compare — (HH, MM) > (15, 30) iff strictly later
    return (now_ist.hour, now_ist.minute) > (15, 30)


def _make_logged_compute(
    base_callable,
    *,
    db_engine,
    exchange: str,
    is_eod_fn,
    snapshotter,
):
    """Step 13a: wrap the compute_run_once callable to:
      1. Record one compute_runs row per tick (kind=intraday/eod).
      2. After a successful EoD tick, write a market_overview_daily
         snapshot for the IST-current date.

    Failures in (1) or (2) are logged but never propagated — the
    orchestrator's downstream consumers (distribute step, pulse
    publish) must not be blocked by an activity-log write failing.

    The wrapper layers on TOP of Step 11a's deferring layer:
      base (no log) → deferring (skips if backfill active) →
      logged (this layer) → orchestrator pool
    """
    from .persistence.compute_runs_repo import ComputeRunsRepository

    def _logged():
        kind = "eod" if is_eod_fn() else "intraday"
        run_id: int | None = None
        try:
            with db_engine.begin() as conn:
                run_id = ComputeRunsRepository(conn).start(
                    kind=kind, exchange=exchange,
                )
        except Exception as log_err:
            # Activity-log write failure must not block the
            # compute pass. Log and proceed.
            logger.warning(
                "compute_runs.start failed (kind=%s, exchange=%s): "
                "%s — continuing without an activity-log row",
                kind, exchange, log_err, exc_info=True,
            )

        result = None
        error_message: str | None = None
        try:
            result = base_callable()
            return result
        except Exception as e:
            error_message = repr(e)
            raise
        finally:
            # Update the compute_runs row (best-effort)
            if run_id is not None:
                try:
                    with db_engine.begin() as conn:
                        ComputeRunsRepository(conn).finish(
                            run_id,
                            error_message=error_message,
                        )
                except Exception as log_err:
                    logger.warning(
                        "compute_runs.finish failed "
                        "(run_id=%d): %s — row will show as "
                        "in-flight on next read",
                        run_id, log_err, exc_info=True,
                    )

            # EoD snapshot: only fire when the compute pass
            # succeeded (snapshot reads what compute wrote)
            if kind == "eod" and error_message is None:
                try:
                    snapshotter.snapshot_for_today(
                        exchange=exchange,
                    )
                except Exception as snap_err:
                    logger.warning(
                        "EoD market_overview snapshot failed "
                        "(exchange=%s): %s",
                        exchange, snap_err, exc_info=True,
                    )

    return _logged


# === STEP 14B-E6-boc_sig: scheduler/distributor params removed ===
def _build_orchestrator_components(
    *,
    config,
    db_engine,
    v1_cache: CurrentSignalsCache,
) -> tuple[PulseState | None, OrchestratorPool | None]:
    """Phase 14: build (PulseState, OrchestratorPool) if enabled in config.

    Returns (None, None) when `scheduling.orchestrator.enabled = False` —
    the legacy ComputeScheduler thread is in charge and the new HTTP
    endpoints return 503. When enabled:

      * `PulseState` is constructed (its asyncio loop binding happens
        later, during FastAPI's lifespan startup, when the loop is
        actually running).
      * `OrchestratorPool` is built from `config.market_hours`. The
        pool's threads are NOT started here — `start_all()` is called
        from the lifespan startup callback for the same loop-binding
        reason.
      * The `fetch_fn` for each market closes over (db_engine, config)
        and calls `run_eod(..., exchange=...)` — the natural fetch
        primitive. If you need a different cadence (intraday, custom),
        change the closure here.

    Failure modes:
      * Config validation (market_hours empty, tz invalid, etc.) raises
        from `build_pool_from_configs`. Surfaced at startup with the
        original Pydantic error.
      * fetch_fn construction is lazy — the runner functions are
        imported at call time so a broken yfinance install doesn't
        block service startup for non-fetch tests.
    """
    if not config.scheduling.orchestrator.enabled:
        logger.info("Orchestrator disabled in config; skipping pool build")
        return (None, None)

    if not config.market_hours:
        logger.warning(
            "Orchestrator enabled but config.market_hours is empty; "
            "no per-market threads will run. Pool returned empty.",
        )

    # Local import: pulse_state lives under transports.http to keep the
    # SSE-specific concerns close to the HTTP route that consumes them.
    from .transports.http.pulse_state import PulseState

    pulse_state = PulseState()

    orch_cfg = config.scheduling.orchestrator
    settings = OrchestratorSettings(
        fetch_timeout=timedelta(seconds=orch_cfg.fetch_timeout_seconds),
        fetch_retry_delay=timedelta(
            minutes=orch_cfg.fetch_retry_delay_minutes,
        ),
        catch_up_on_startup=orch_cfg.catch_up_on_startup,
        catch_up_skip_if_recent=timedelta(
            minutes=orch_cfg.catch_up_skip_if_recent_minutes,
        ),
    )

    def _fetch_fn_for(exchange: str):
        """Closure: each market's orchestrator gets a thin wrapper
        that calls `run_eod` with the right exchange code. Imported
        lazily so the etl extras don't have to be installed for
        non-fetch tests.

        Phase 14.6a: the runner's first positional parameter is
        `db_engine` (renamed from `engine` for codebase-wide
        consistency); the call below uses kwargs throughout to be
        explicit.
        """
        from .etl.runner import run_eod  # noqa: PLC0415

        def _fetch(exchange_str: str):
            # The orchestrator passes its own exchange string; we
            # honor it (rather than the closure-captured one) so a
            # future per-market refactor wouldn't silently misroute.
            assert exchange_str == exchange, (
                f"fetch_fn for {exchange} invoked with {exchange_str}"
            )
            result = run_eod(
                db_engine=db_engine,
                config=config,
                exchange=exchange,
            )
            # === STEP 14A-FIXUP: legacy eod_pipeline call removed ===
            # The post-fetch fundamentals pipeline call has been
            # demolished as part of the V1 cutover — recompute_market
            # (invoked separately by the orchestrator as
            # compute_run_once) now handles all indicators.
            return result
        return _fetch

    # Step 10 cutover: V1 lambdas. The legacy
    # `compute_scheduler.run_once` and `distributor.run_once` are no
    # longer reachable from the orchestrator — ComputeScheduler is
    # constructed but neither `.start()`'d nor `.run_once()`'d.
    # Step 14 demolishes both.
    base_compute_run_once = _make_v1_compute_run_once(
        db_engine=db_engine, v1_cache=v1_cache,
    )
    base_distribute_run_once = _make_v1_distribute_run_once(
        db_engine=db_engine,
        brevo_config=config.transports.brevo,
    )

    # Step 11a: deferring layer. When `backfill_runs.is_running()`
    # returns True, ticks no-op and a background poller spawns to
    # run one catch-up tick after backfill completes.
    poller_state = _BackfillPollerState()
    compute_run_once = _make_deferring_compute(
        base_callable=base_compute_run_once,
        db_engine=db_engine,
        poller_state=poller_state,
        catch_up_distribute=base_distribute_run_once,
    )
    distribute_run_once = _make_deferring_distribute(
        base_callable=base_distribute_run_once,
        db_engine=db_engine,
    )

    # Step 13a: log every compute tick into compute_runs + write
    # an EoD market_overview_daily snapshot. Wraps the deferring
    # layer's output so deferring still applies (when a backfill
    # holds the lock, the tick no-ops BEFORE we'd log it — that's
    # correct, deferred ticks shouldn't pollute the activity log).
    from .snapshots.market_overview import MarketOverviewSnapshotter
    _snapshotter = MarketOverviewSnapshotter(db_engine)
    compute_run_once = _make_logged_compute(
        compute_run_once,
        db_engine=db_engine,
        exchange="NSE",
        is_eod_fn=_is_eod_now_nse,
        snapshotter=_snapshotter,
    )

    pool = build_pool_from_configs(
        market_configs=config.market_hours,
        settings=settings,
        fetch_fn_factory=_fetch_fn_for,
        compute_run_once=compute_run_once,
        distribute_run_once=distribute_run_once,
        pulse=pulse_state,
    )
    logger.info(
        "Built OrchestratorPool with %d market(s): %s",
        len(pool), pool.exchanges(),
    )
    return (pulse_state, pool)


def _build_phase1a_batch_subsystem(*, db_engine):
    """PHASE-1A-DISPATCHER-BUILD: construct RunQueue + RunDispatcher.

    Idempotent-ish: constructing is cheap; the dispatcher thread
    is not started here (caller does that in the lifespan startup).

    Also clears any stale update_run_in_progress rows left by a
    crashed prior process — see the Q8 rationale in the design
    thread.
    """
    # PHASE-1A-CLEAR-INPROG-AT-BOOT: startup cleanup.
    with _txn_for_phase1a(db_engine) as conn:
        n = UpdateRunInProgressRepository(conn).clear_all()
    if n:
        logger.warning(
            "Phase 1a boot: cleared %d stale update_run_in_progress "
            "row(s) from a prior process crash", n,
        )
    else:
        logger.info(
            "Phase 1a boot: update_run_in_progress table already empty",
        )

    queue = RunQueue()
    dispatcher = RunDispatcher(db_engine=db_engine, queue=queue)
    logger.info(
        "Phase 1a batch subsystem constructed (dispatcher not started yet)",
    )
    return queue, dispatcher


def _build_redis_queue(redis_config):
    """Build a RedisQueue from RedisConfig. Password (if configured)
    is read from the env var named in `redis_config.password_env`."""
    import os
    import redis
    from .notifications.redis_queue import RedisQueue

    password = None
    if redis_config.password_env:
        password = os.environ.get(redis_config.password_env)
    client = redis.Redis(
        host=redis_config.host,
        port=redis_config.port,
        db=redis_config.db,
        password=password,
        decode_responses=False,
    )
    return RedisQueue(
        redis_client=client,
        queue_name=redis_config.queue_name,
        dedup_hash_name=redis_config.dedup_hash_name,
    )


def _wrap_transports_with_breakers(
    transports: dict[str, NotificationTransport],
    config,
) -> dict[str, NotificationTransport]:
    """If circuit-breakers are enabled in config, wrap each transport
    with a `CircuitBreakingTransport`. Otherwise return as-is.

    Each transport gets its OWN breaker instance so an unhealthy SMTP
    server doesn't trip the webhook breaker. The breaker name (used in
    metric labels) is the transport key — "email", "webhook", etc.
    """
    cb_cfg = config.transports.circuit_breaker
    if not cb_cfg.enabled:
        return transports

    from .notifications.circuit_breaker import (
        CircuitBreakerConfig as BreakerConfig,
        CircuitBreakingTransport,
    )
    bcfg = BreakerConfig(
        failure_threshold=cb_cfg.failure_threshold,
        reset_timeout_seconds=cb_cfg.reset_timeout_seconds,
        half_open_max_successes=cb_cfg.half_open_max_successes,
    )
    return {
        name: CircuitBreakingTransport(t, name=name, config=bcfg)
        for name, t in transports.items()
    }


def _make_v1_compute_run_once(
    *,
    db_engine,
    v1_cache: CurrentSignalsCache,
    exchange: str = "NSE",
):
    """Factory: build the V1 `compute_run_once` callable that the
    orchestrator's pipeline invokes once per tick.

    Closes over `db_engine` and `v1_cache`. `exchange` defaults to
    'NSE' — the V1 cycle is NSE-only for now; BSE/NYSE wiring is
    Phase 14.7+ work. Other callers (e.g. backfill in Step 11) can
    pass a different exchange.

    The returned callable takes no args and returns the
    `RecomputeMarketResult` from `recompute_market`. The orchestrator's
    pipeline machinery stores the result for `/admin/health` snapshots.
    """
    def _run():
        return recompute_market(
            db_engine,
            mode="live",
            cache=v1_cache,
            exchange=exchange,
        )
    return _run


def _make_v1_distribute_run_once(
    *,
    db_engine,
    brevo_config,
):
    """Factory: build the V1 `distribute_run_once` callable that the
    orchestrator's pipeline invokes once per tick (after the compute
    step finishes).

    When `brevo_config` is None (transports.brevo not configured),
    returns a no-op lambda that returns None. This is the "Brevo
    deliberately disabled" path — the cycle runs to completion, no
    emails go out, no error is logged each tick.

    When configured, the returned callable:
      1. Generates a per-tick cycle_id from the current wall clock
         (UTC-anchored YYYYMMDD-HHMM for deterministic ordering
         across pods/markets).
      2. Calls `notify_subscribers` with the Brevo credentials and
         the current wall clock.
      3. Returns the `NotifyResult` for the orchestrator's pipeline
         to capture in its tick record.
    """
    if brevo_config is None:
        logger.warning(
            "V1 distribute: transports.brevo not configured — "
            "notify_subscribers will be a no-op each tick. "
            "Set transports.brevo in config to enable emails.",
        )
        def _noop():
            return None
        return _noop

    # Re-load the API key the same way EmailTransport does. The key
    # is short-lived in memory; we capture it into this closure
    # rather than re-reading the file on every tick.
    from .config.brevo_secrets import load_brevo_api_key
    api_key = load_brevo_api_key(brevo_config.secrets_file)
    from_address = brevo_config.sender_email
    from_name = brevo_config.sender_name

    logger.info(
        "V1 distribute: wired with Brevo (sender=%s)",
        from_address,
    )

    def _run():
        now = datetime.now(UTC)
        cycle_id = now.strftime("%Y%m%d-%H%M")
        return notify_subscribers(
            db_engine,
            brevo_api_key=api_key,
            brevo_from_address=from_address,
            brevo_from_name=from_name,
            cycle_id=cycle_id,
            now=now,
        )
    return _run


# =====================================================================
# Backfill marker / deferred tick (V1 cutover Step 11a)
# =====================================================================
# When a backfill is active (a row in `backfill_runs` with
# `finished_at IS NULL`), the orchestrator's tick must not race against
# the backfill's writes to indicator_state / signals / cache. Instead,
# tick stages no-op and a background polling thread (15-min cadence)
# runs ONE catch-up tick after the backfill completes.
#
# Dedup invariant: only one polling thread exists at a time. The orches-
# trator may fire multiple ticks during a multi-hour backfill (4x daily
# NSE schedule); each tick checks `state.active` under the lock and
# bails out if a poller is already running.

_BACKFILL_POLL_INTERVAL_SECONDS: float = 15 * 60  # 15 min


@dataclass
class _BackfillPollerState:
    """Per-orchestrator-pool poller dedup state. One instance lives
    in the `_build_orchestrator_components` scope, shared between
    the deferring compute + distribute wrappers."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    active: bool = False


def _spawn_backfill_poller(
    *,
    db_engine,
    state: _BackfillPollerState,
    catch_up_compute,
    catch_up_distribute,
    poll_interval_seconds: float = _BACKFILL_POLL_INTERVAL_SECONDS,
) -> threading.Thread | None:
    """Spawn one polling thread (if none is active) that waits for
    the active backfill to finish, then runs one catch-up
    compute + distribute pair.

    Returns the spawned Thread, or None if a poller was already
    active (dedup case).
    """
    with state.lock:
        if state.active:
            return None
        state.active = True

    def _poll() -> None:
        try:
            while True:
                time.sleep(poll_interval_seconds)
                with db_engine.connect() as conn:
                    still_running = BackfillRunsRepository(
                        conn,
                    ).is_running()
                if not still_running:
                    break
                logger.info(
                    "backfill-poller: backfill still active; "
                    "sleeping %.0fs",
                    poll_interval_seconds,
                )
            logger.info(
                "backfill-poller: backfill finished; running "
                "one catch-up tick (compute + distribute)",
            )
            try:
                catch_up_compute()
            except Exception:
                logger.exception(
                    "backfill-poller: catch-up compute failed",
                )
            try:
                catch_up_distribute()
            except Exception:
                logger.exception(
                    "backfill-poller: catch-up distribute failed",
                )
        finally:
            with state.lock:
                state.active = False

    thread = threading.Thread(
        target=_poll,
        name="backfill-poller",
        daemon=True,
    )
    thread.start()
    return thread


def _make_deferring_compute(
    *,
    base_callable,
    db_engine,
    poller_state: _BackfillPollerState,
    catch_up_distribute,
) -> "Callable[[], object]":
    """Wraps the Step 10 V1 compute callable with a marker check.

    On each invocation:
      - If `backfill_runs.is_running()` is True: spawn the poller
        (if one isn't already running) and return None
      - Else: invoke `base_callable()` and return its result

    The compute factory is the one responsible for spawning the
    poller because the orchestrator's tick calls compute first;
    distribute's deferring wrapper just no-ops on deferral.
    """
    def _run():
        with db_engine.connect() as conn:
            running = BackfillRunsRepository(conn).is_running()
        if running:
            _spawn_backfill_poller(
                db_engine=db_engine,
                state=poller_state,
                catch_up_compute=base_callable,
                catch_up_distribute=catch_up_distribute,
            )
            logger.info(
                "compute tick deferred: backfill is running",
            )
            return None
        return base_callable()
    return _run


def _make_deferring_distribute(
    *,
    base_callable,
    db_engine,
) -> "Callable[[], object]":
    """Wraps the Step 10 V1 distribute callable with a marker check.

    On each invocation:
      - If `backfill_runs.is_running()` is True: return None
      - Else: invoke `base_callable()` and return its result

    No poller spawned here — the compute wrapper handles that.
    """
    def _run():
        with db_engine.connect() as conn:
            running = BackfillRunsRepository(conn).is_running()
        if running:
            logger.info(
                "distribute tick deferred: backfill is running",
            )
            return None
        return base_callable()
    return _run


def _build_email_transport_or_null(config, *, enrichment_provider=None, db_engine=None):


    """Build the email transport from `config.transports.brevo`.

    Returns NullTransport when `transports.brevo` is None (the
    explicit "email disabled" choice for dev/test). When it IS set,
    returns a fully-configured Brevo HTTP API EmailTransport.

    Note: there is NO env-var fallback path. The previous SMTP
    implementation silently substituted NullTransport when
    TRIP_EMA_SMTP_HOST was unset — a bug class that lost weeks of
    notifications. The new contract is: config controls delivery,
    secrets file controls the credential, both validated at startup
    by load_config().

    `enrichment_provider` is optional. When passed, the EmailTransport
    invokes it on each send() to fetch per-ticker enrichment for HTML
    rendering (mcap, sector, sector zone/direction, last 5 signals).
    When None, sends are text-only (the legacy behavior).
    """
    brevo_cfg = config.transports.brevo
    if brevo_cfg is None:
        logger.info(
            "transports.brevo not configured — using NullTransport for email"
        )
        return NullTransport()

    # load_config() already validated this file at startup. Re-loading
    # here is the simplest path to a fresh string (we never store the
    # secret on the Config object) and is cheap (one small file read).
    from .config.brevo_secrets import load_brevo_api_key
    api_key = load_brevo_api_key(brevo_cfg.secrets_file)

    logger.info(
        "Email transport: Brevo HTTP API (sender=%s, endpoint=%s)",
        brevo_cfg.sender_email, brevo_cfg.api_url,
    )
    return EmailTransport(
        api_key=api_key,
        from_address=brevo_cfg.sender_email,
        from_name=brevo_cfg.sender_name,
        subject=brevo_cfg.subject,
        api_url=brevo_cfg.api_url,
        timeout_seconds=brevo_cfg.timeout_seconds,
        enrichment_provider=enrichment_provider,
        _db_engine=db_engine,
    )


def _build_shutdown_callback(state: AppState, notifier_proc=None):
    """Return a zero-arg callable that performs the graceful shutdown
    sequence. The callable is registered with uvicorn via FastAPI's
    lifespan; uvicorn invokes it as part of its own SIGTERM/SIGINT
    handling, so this runs BEFORE the process exits.

    Order matters:
      1. SubscriberDistributor first — stops generating new jobs.
      2. ComputeScheduler — stops generating new signals.
      3. Notifier subprocess (if spawned) — SIGTERM, then wait up to
         `http.notifier_subprocess_shutdown_timeout_seconds` for clean
         shutdown of in-flight jobs. Escalates to SIGKILL after.
      4. DB engine — dispose connections.

    This ordering minimizes the window where a signal is computed but
    no transport completes for it.
    """
    join_timeout = state.config.http.notifier_subprocess_shutdown_timeout_seconds

    def _shutdown() -> None:
        logger.info("Lifespan shutdown: stopping background threads")
        # Phase 14: stop the orchestrator pool FIRST. If we stopped
        # the distributor before the pool, an in-flight orchestrator
        # tick could try to call distributor.run_once() on a stopped
        # distributor and hit a confusing state. Stopping the pool
        # also lets us emit `orchestrator_stopped` pulse events
        # before the consumers go away.
        try:
            if state.orchestrator_pool is not None:
                state.orchestrator_pool.stop_all(
                    timeout_per_orchestrator=10.0,
                )
        except Exception:
            logger.exception("Error stopping orchestrator_pool")
        # === STEP 14B-E6-shutdown: distributor + compute_scheduler stops removed ===
        if notifier_proc is not None and notifier_proc.is_alive():
            logger.info("Terminating notifier worker (pid=%d)", notifier_proc.pid)
            try:
                notifier_proc.terminate()
                notifier_proc.join(timeout=join_timeout)
                if notifier_proc.is_alive():
                    logger.warning(
                        "Notifier worker (pid=%d) did not exit within %ds; killing",
                        notifier_proc.pid, join_timeout,
                    )
                    notifier_proc.kill()
            except Exception:
                logger.exception("Error terminating notifier worker")
        # Phase 10: stop the metrics HTTP server before disposing the DB
        # engine so an in-flight scrape doesn't see closed connections.
        try:
            if state.metrics_server is not None:
                state.metrics_server.stop()
        except Exception:
            logger.exception("Error stopping metrics_server")
        # Phase 11 Slice 2: shut the ETL executor. wait=False so
        # we don't block on a backfill that might take hours; in-flight
        # work writes its own etl_runs row and can resume on next start.
        try:
            if state.etl_executor is not None:
                state.etl_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.exception("Error shutting down etl_executor")
        try:
            state.db_engine.dispose()
        except Exception:
            logger.exception("Error disposing db_engine")
        logger.info("Lifespan shutdown complete")
    return _shutdown


def _build_startup_callback(state: AppState):
    """Return a zero-arg callable that performs Phase 14 startup tasks
    requiring a running asyncio event loop.

    Two things happen here, in order:

      1. **Bind PulseState to the loop.** PulseState's `publish()` is
         called from the orchestrator's worker thread; the fan-out to
         async SSE subscribers needs `loop.call_soon_threadsafe(...)`,
         which needs the loop reference. We capture it here, inside
         the lifespan's startup phase, where `asyncio.get_running_loop()`
         is meaningful.

      2. **Start the OrchestratorPool.** Each MarketOrchestrator's
         daemon thread is spawned via pool.start_all(). The pool
         emits `orchestrator_started` pulse events; with the loop
         already bound, those reach any GUI client that connects to
         /v1/pulse/stream during the boot window.

    When the orchestrator is disabled in config (pool=None, pulse=None),
    this is a no-op. The callback is still wired so the lifespan
    path is uniform across deployment modes.
    """
    import asyncio  # local to keep import graph tidy

    def _startup() -> None:
        if state.pulse_state is None or state.orchestrator_pool is None:
            logger.info(
                "Phase 14 startup: orchestrator disabled; skipping",
            )
            return
        try:
            loop = asyncio.get_running_loop()
            state.pulse_state.bind_loop(loop)
            logger.info("PulseState bound to asyncio event loop")
        except RuntimeError:
            # No running loop — shouldn't happen during lifespan
            # startup, but defensive: log and continue. SSE fan-out
            # will be a no-op; snapshot still works.
            logger.exception(
                "PulseState could not bind event loop; SSE fan-out "
                "will be disabled (snapshot endpoint still works).",
            )
        try:
            state.orchestrator_pool.start_all()
            logger.info(
                "OrchestratorPool started with %d market(s): %s",
                len(state.orchestrator_pool),
                state.orchestrator_pool.exchanges(),
            )
        except Exception:
            logger.exception("Error starting orchestrator_pool")

        # PHASE-1A-DISPATCHER-START: start the batch-run dispatcher.
        if getattr(state, "run_dispatcher", None) is not None:
            try:
                state.run_dispatcher.start()
                logger.info("Phase 1a RunDispatcher started")
            except Exception:
                logger.exception("Error starting RunDispatcher")
    return _startup


def _install_signal_handlers(shutdown_callback) -> None:
    """Belt-and-suspenders signal handling.

    When `uvicorn.run()` runs, uvicorn installs its own SIGINT/SIGTERM
    handlers and they overwrite anything we register here. uvicorn's
    handlers trigger the ASGI lifespan shutdown, which calls our
    shutdown_callback — so the graceful path works through uvicorn,
    not through these handlers.

    These handlers ONLY fire if:
      - The process receives a signal before uvicorn.run() is reached
        (e.g., during boot), OR
      - We're running outside uvicorn (e.g., in a test harness).

    They call the same callback as a safety net so the shutdown
    sequence stays consistent across all paths.
    """
    invoked = {"value": False}

    def _shutdown(signum, frame):
        if invoked["value"]:
            return  # idempotent — already shutting down
        invoked["value"] = True
        logger.info("Signal %s received outside uvicorn; running shutdown_callback", signum)
        try:
            shutdown_callback()
        except Exception:
            logger.exception("Error in signal-driven shutdown")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)


# === STEP 14B-E6-spawn_fn: _spawn_notifier_process removed (notifier_worker.py deleted) ===


def main(argv: list[str] | None = None) -> NoReturn:
    parser = argparse.ArgumentParser(prog="trip_ema")
    parser.add_argument(
        "--config", "-c", type=Path, required=True,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    # === STEP 14B-E6-spawn_arg: --spawn-notifier removed ===
    args = parser.parse_args(argv)

    # Bootstrap logging in text mode so the early boot lines are
    # readable. After the config loads we'll re-configure based on the
    # user's preference (log_json), which is idempotent.
    _configure_logging(args.log_level, json_output=False)
    state = _build_app_state(args.config)
    # Apply the user's preferred log format (json vs text). The
    # observability.configure_logging is idempotent: repeated calls
    # leave one handler at the end, with the most recent settings.
    # This replaces the bootstrap handler set above.
    _configure_logging(args.log_level, json_output=state.config.observability.log_json)
    set_state(state)

    # === STEP 14B-E6-spawn_use: notifier subprocess spawn removed ===
    # Build the graceful-shutdown callback ONCE; share it between the
    # ASGI lifespan (the path uvicorn uses on SIGTERM/SIGINT) and the
    # explicit signal handlers (the fallback for non-uvicorn paths).
    shutdown_callback = _build_shutdown_callback(state, notifier_proc=None)
    _install_signal_handlers(shutdown_callback)

    # Phase 14: build the startup callback that binds PulseState to
    # the asyncio loop and starts the OrchestratorPool. No-op when
    # orchestrator is disabled in config.
    startup_callback = _build_startup_callback(state)

    app = create_app(
        shutdown_callback=shutdown_callback,
        startup_callback=startup_callback,
    )

    # Run uvicorn synchronously. uvicorn manages its own signal handling
    # in the asyncio loop; the ASGI lifespan we registered fires our
    # shutdown_callback during uvicorn's graceful shutdown.
    import uvicorn
    config = state.config
    logger.info(
        "Starting HTTP server on %s:%d", config.http.host, config.http.port,
    )
    uvicorn.run(
        app, host=config.http.host, port=config.http.port,
        log_config=None,        # we configured logging above
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
