"""Main entrypoint for trip_ema.

Boot sequence:
    1. Parse CLI args (config path, dev mode flag)
    2. Configure logging
    3. Load + validate config
    4. Open DB engine
    5. Apply pending migrations
    6. Build V1 CurrentSignalsCache; prime from current_signals
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
from pathlib import Path
from typing import NoReturn

# PHASE-3B: PulseState TYPE_CHECKING import removed (module deleted).

# PHASE-1A-DISPATCHER-BUILD: new batch-run subsystem imports.
from .persistence.update_run_in_progress_repo import (
    UpdateRunInProgressRepository,
)
from .persistence import transaction as _txn_for_phase1a
from .scheduler.run_dispatcher import RunDispatcher
from .scheduler.run_queue import RunQueue
# PHASE-3A: RunScheduler + calendar imports for the tick planner.
from .scheduler.run_scheduler import RunScheduler
from .scheduler.market_calendar import TradingCalendar
from sqlalchemy import text as _sa_text

# PHASE-3E: SignalCache import removed (class deleted).
from .cache.current_signals_cache import CurrentSignalsCache
from .config import load_config
from .notifications import EmailTransport, NullTransport
from .notifications.base import NotificationTransport
from .persistence import (
    create_engine_from_config,
)
from .persistence.current_signals_repo import CurrentSignalsRepository
# === STEP 14B-E6-import: ComputeScheduler + SubscriberDistributor imports stripped ===
# PHASE-3B: OrchestratorPool + OrchestratorSettings + build_pool_from_configs
# imports removed; the classes were deleted alongside market_orchestrator.py
# and orchestrator_pool.py.
from .scheduler import MarketHoursService
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

    # PHASE-3E: Legacy SignalCache construction + prime_cache_from_db call
    # removed. The class is deleted; the V1 CurrentSignalsCache below is
    # the only cache the service maintains.

    logger.info("Building V1 current_signals cache")
    v1_cache = CurrentSignalsCache()
    with db_engine.connect() as conn:
        v1_signals = CurrentSignalsRepository(conn).list_all()
    v1_cache.rebuild_from(v1_signals)
    logger.info(
        "V1 cache primed: %d signals", v1_cache.signal_count(),
    )

    # === STEP 14B-E6-cs_build: ComputeScheduler construction removed ===
    # The orchestrator pool (Phase 14) owns all compute cadence in V1, so the
    # resulting object is unused here.
    #
    # The CALL is deliberately kept: MarketHoursService.__init__ is not
    # side-effect free — it walks the config calling _parse_hhmm() (raises on a
    # malformed "HH:MM") and ZoneInfo() (raises on an unknown timezone). This
    # line is therefore a STARTUP FAIL-FAST on the market_hours config. Deleting
    # it, which is what a careless F841 fix does, would let a typo'd open/close
    # time or tz name boot cleanly and blow up later somewhere unrelated.
    MarketHoursService(config.market_hours)  # noqa: F841 — validates config

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

    # PHASE-3B: _build_orchestrator_components call removed.
    # MarketOrchestrator + OrchestratorPool + PulseState are gone;
    # RunScheduler + RunDispatcher own scheduling entirely.

    # PHASE-1A + PHASE-3A: build queue + dispatcher + scheduler.
    # Threads are not started here — the lifespan startup callback
    # does that, gated by config.scheduling.dispatcher_enabled for
    # the scheduler specifically.
    run_queue, run_dispatcher, run_scheduler = (
        _build_phase1a_batch_subsystem(
            db_engine=db_engine,
            config=config,           # PHASE-3A-BATCH-CALLSITE
            config_path=config_path, # PHASE-3A-BATCH-CALLSITE
        )
    )

    # === STEP 14B-FIXUP1-app-appstate: v1_cache plumbed through ===
    # PHASE-3B-APPSTATE: orchestrator_pool + pulse_state kwargs removed
    # (fields deleted from AppState alongside their subsystems).
    # PHASE-3E: cache=cache kwarg removed (AppState.cache field deleted).
    return AppState(
        config=config,
        db_engine=db_engine,
        v1_cache=v1_cache,
        redis_queue=redis_queue,
        metrics_server=metrics_server,
        etl_executor=etl_executor,
        run_queue=run_queue,               # PHASE-1A-BATCH-CALLSITE
        run_dispatcher=run_dispatcher,     # PHASE-1A-BATCH-CALLSITE
        run_scheduler=run_scheduler,       # PHASE-3A-BATCH-CALLSITE
    )


# PHASE-3B: Deleted _is_eod_now_nse, _make_logged_compute, and
# _build_orchestrator_components. All three were part of MarketOrchestrator's
# in-process tick pipeline (Step 13a + Phase 14). RunScheduler + RunDispatcher
# spawn cmd_cycle subprocesses now; the ETL CLI (trip_ema/etl/cli.py::cmd_cycle)
# owns the fetch->compute->distribute pipeline directly.


def _build_phase1a_batch_subsystem(*, db_engine, config=None, config_path=None):
    """PHASE-1A + PHASE-3A: construct RunQueue + RunDispatcher + RunScheduler.

    Phase 1a: builds the queue + dispatcher, clears stale
    update_run_in_progress markers from prior crashes.

    Phase 3a additions:
      - Also clears orphan compute_runs rows (finished_at IS NULL)
        from processes that died before Phase 2's SIGTERM handler
        could clean up. Applies unconditionally so the UI doesn't
        get wedged into thinking a dead run is still active.
      - Constructs RunScheduler (wall-clock tick planner). Not
        .start()'d — the lifespan startup callback starts it only
        if config.scheduling.dispatcher_enabled is True. When
        config/config_path are None (legacy callers), the scheduler
        is None — caller gets a 2-tuple for backward compat.

    The signature accepts config + config_path as optional kwargs so
    older test callers that only pass db_engine keep working (they
    just don't get a RunScheduler).
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

    # PHASE-3A-CLEAR-ORPHAN-COMPUTE-RUNS: mark orphan compute_runs
    # rows (finished_at IS NULL from crashed processes) as cancelled.
    # Column names verified against DDL.txt: compute_run_id, exchange,
    # kind, failed_stage (varchar 64), error_message (text).
    with _txn_for_phase1a(db_engine) as conn:
        result = conn.execute(_sa_text("""
            UPDATE compute_runs
               SET finished_at = NOW(),
                   failed_stage = 'boot_orphan_cleanup',
                   error_message = 'process died without SIGTERM handler (pre-Phase-2 or crash)'
             WHERE finished_at IS NULL
         RETURNING compute_run_id, exchange, kind
        """))
        orphans = list(result)
    if orphans:
        logger.warning(
            "Phase 3a boot: cleaned up %d orphan compute_runs rows: %s",
            len(orphans),
            [
                (r.compute_run_id, r.exchange, r.kind)
                for r in orphans
            ],
        )
    else:
        logger.info(
            "Phase 3a boot: no orphan compute_runs rows to clean up",
        )

    queue = RunQueue()
    dispatcher = RunDispatcher(db_engine=db_engine, queue=queue)

    # PHASE-3A: build RunScheduler if we have config to derive calendars
    # from. Calendars are built once at boot (a dict keyed by exchange).
    # Adding a new exchange to config.yaml at runtime will NOT be picked
    # up until service restart — logged in the factory closure.
    scheduler = None
    if config is not None and config_path is not None:
        calendars: dict[str, TradingCalendar] = {
            mh.exchange: TradingCalendar(mh.to_calendar_config())
            for mh in config.market_hours
        }

        def _calendar_factory(exchange: str) -> TradingCalendar:
            if exchange not in calendars:
                raise KeyError(
                    f"No calendar for exchange {exchange!r}; "
                    f"configured exchanges: {sorted(calendars.keys())}. "
                    f"Restart service to pick up new exchanges."
                )
            return calendars[exchange]

        scheduler = RunScheduler(
            queue=queue,
            config_path=config_path,
            calendar_factory=_calendar_factory,
        )
        logger.info(
            "Phase 3a RunScheduler constructed for %d exchange(s): %s",
            len(calendars), sorted(calendars.keys()),
        )
    else:
        logger.info(
            "Phase 3a: RunScheduler skipped (no config/config_path passed; "
            "test caller or legacy path)",
        )

    logger.info(
        "Phase 1a+3a batch subsystem constructed "
        "(dispatcher + scheduler not started yet)",
    )
    return queue, dispatcher, scheduler


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


# PHASE-3B: Deleted _make_v1_compute_run_once, _make_v1_distribute_run_once,
# _BackfillPollerState, _make_deferring_compute, _make_deferring_distribute.
# These were the injection callables MarketOrchestrator's per-market pipeline
# consumed. All the actual work they wrapped (recompute_market, notify_subscribers,
# BackfillRunsRepository.is_running checks) now happens inside cmd_cycle.
# BackfillRunsRepository defers the same way there; recompute_market runs
# directly in the subprocess.


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
        # PHASE-3B-SHUTDOWN: orchestrator_pool.stop_all() block removed
        # (subsystem deleted). RunDispatcher + RunScheduler handle their own
        # stop paths below via their .stop() methods when we shut down their
        # threads (currently daemon threads, so they die with the process,
        # but explicit .stop() is nicer for tests + graceful shutdown).
        logger.info("Lifespan shutdown: stopping background threads")
        try:
            if getattr(state, "run_scheduler", None) is not None:
                state.run_scheduler.stop()
        except Exception:
            logger.exception("Error stopping run_scheduler")
        try:
            if getattr(state, "run_dispatcher", None) is not None:
                state.run_dispatcher.stop()
        except Exception:
            logger.exception("Error stopping run_dispatcher")
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
    """Return a zero-arg callable that starts Phase 1a RunDispatcher +
    Phase 3a RunScheduler during the ASGI lifespan startup.

    PHASE-3B: PulseState + OrchestratorPool startup was removed here;
    both subsystems have been deleted. Threads that need the asyncio
    loop to already exist are still started from this callback rather
    than during _build_app_state, because the lifespan startup runs
    inside the running loop while _build_app_state does not.

    Both start blocks are guarded on `is not None` and wrapped in
    try/except so a broken thread doesn't abort startup — failures
    are logged and the app continues with the healthy subsystem(s).
    """
    def _startup() -> None:
        # PHASE-3B-STARTUP: PulseState + OrchestratorPool blocks removed.
        # RunScheduler + RunDispatcher are the only scheduling subsystems
        # now. Both start unconditionally — the dispatcher_enabled flag
        # is gone (Phase 3a was a rollout gate; Phase 3b makes it the
        # only behavior).

        # PHASE-1A-DISPATCHER-START: start the batch-run dispatcher.
        if getattr(state, "run_dispatcher", None) is not None:
            try:
                state.run_dispatcher.start()
                logger.info("Phase 1a RunDispatcher started")
            except Exception:
                logger.exception("Error starting RunDispatcher")

        # PHASE-3B-SCHEDULER-START: start the wall-clock RunScheduler
        # unconditionally (no more dispatcher_enabled gate).
        if getattr(state, "run_scheduler", None) is not None:
            try:
                state.run_scheduler.start()
                logger.info("Phase 3b RunScheduler started")
            except Exception:
                logger.exception("Error starting RunScheduler")
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
    # PHASE-3A-STDERR-LINEBUF: force line-buffered stderr + stdout so
    # log records emitted during _build_app_state land in the redirected
    # log file promptly. restart_service.sh runs us under nohup with
    # `> /tmp/trip-ema.log 2>&1`, which on some environments makes
    # Python's stderr block-buffered — boot lines would sit in the
    # buffer until uvicorn's later output filled it, so restart_service.sh's
    # log overwrite (next restart) lost them. Line buffering guarantees
    # each log record flushes on '\n'.
    try:
        sys.stderr.reconfigure(line_buffering=True)
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        # Older Python without reconfigure, or streams already
        # torn down. Best-effort — the log-visibility gain is
        # nice-to-have, not load-bearing.
        pass

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
    # PHASE-3A-STDERR-FLUSH: belt-and-suspenders — force any queued
    # boot log lines to disk before uvicorn takes over. Line buffering
    # (set at top of main) should already have done this per-record,
    # but a redundant flush costs nothing and is diagnostic-friendly.
    try:
        sys.stderr.flush()
    except Exception:
        pass
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
