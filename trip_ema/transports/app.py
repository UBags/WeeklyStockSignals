"""Main entrypoint for trip_ema.

Boot sequence:
    1. Parse CLI args (config path, dev mode flag)
    2. Configure logging
    3. Load + validate config
    4. Open DB engine
    5. Apply pending migrations
    6. Build TickerRegistry and SignalCache; prime from DB
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

from .api import prime_all_tickers
from .cache import SignalCache
from .config import load_config
from .domain import Timeframe
from .notifications import EmailTransport, NullTransport, WebhookTransport
from .persistence import (
    apply_pending,
    create_engine_from_config,
)
from .registry import TickerRegistry
from .scheduler import (
    ComputeScheduler,
    MarketHoursService,
    SubscriberDistributor,
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

    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    logger.info("Applying pending migrations from %s", migrations_dir)
    applied = apply_pending(db_engine, migrations_dir)
    if applied:
        logger.info("Applied migrations: %s", ", ".join(applied))
    else:
        logger.info("No migrations pending")

    logger.info("Building registry and cache")
    registry = TickerRegistry(
        params=config.to_indicator_params(),
        timeframes=[config.scheduling.default_timeframe],
        history_limit=config.cache.max_history_bars,
        state_history_retain=config.cache.state_history_retain,
    )
    cache = SignalCache()

    logger.info("Priming all tickers from DB")
    result = prime_all_tickers(
        db_engine=db_engine, registry=registry, cache=cache,
    )
    logger.info(
        "Lifecycle: primed=%d, skipped=%d, failed=%d, cache=%d",
        result.prime_result.tickers_primed,
        result.prime_result.tickers_skipped_no_bars,
        result.prime_result.tickers_failed,
        result.cache_entries_populated,
    )

    logger.info("Starting ComputeScheduler (cadence=%d min)",
                config.scheduling.compute_cadence_minutes)
    compute_scheduler = ComputeScheduler(
        db_engine=db_engine,
        registry=registry,
        cache=cache,
        cadence_minutes=config.scheduling.compute_cadence_minutes,
    )
    compute_scheduler.start()

    market_hours = MarketHoursService(config.market_hours)

    # Phase 7: branch on whether Redis is configured.
    # If `config.redis` is set, the distributor operates in queue mode —
    # it enqueues to Redis instead of dispatching transports directly.
    # The actual sends happen in the notifier worker process.
    redis_queue = None
    if config.redis is not None:
        redis_queue = _build_redis_queue(config.redis)
        logger.info(
            "Redis queue configured at %s (queue=%s)",
            config.redis.url_without_password(), config.redis.queue_name,
        )
        distributor = SubscriberDistributor(
            db_engine=db_engine,
            market_hours=market_hours,
            queue=redis_queue,
            cadence_minutes=config.scheduling.subscriber_cadence_minutes,
        )
    else:
        # Legacy in-process mode: transports dispatched from the
        # distributor thread itself. Suitable for dev and small deployments.
        transports = {
            "email": _build_email_transport_or_null(config),
            "webhook": WebhookTransport(),
        }
        transports = _wrap_transports_with_breakers(transports, config)
        distributor = SubscriberDistributor(
            db_engine=db_engine,
            market_hours=market_hours,
            transports=transports,
            cadence_minutes=config.scheduling.subscriber_cadence_minutes,
        )

    distributor.start()
    logger.info(
        "Started SubscriberDistributor (cadence=%d min, mode=%s)",
        config.scheduling.subscriber_cadence_minutes,
        "queue" if redis_queue else "legacy",
    )

    # Phase 10: start Prometheus /metrics server (separate port).
    # Disabled if `config.observability.metrics_port is None`.
    from .observability import start_metrics_server
    from .observability.metrics import set_build_info
    from . import __version__
    set_build_info(__version__)
    metrics_server = start_metrics_server(config.observability.metrics_port)

    return AppState(
        config=config,
        db_engine=db_engine,
        registry=registry,
        cache=cache,
        compute_scheduler=compute_scheduler,
        distributor=distributor,
        redis_queue=redis_queue,
        metrics_server=metrics_server,
    )


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
    transports: dict[str, "NotificationTransport"],
    config,
) -> dict[str, "NotificationTransport"]:
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


def _build_email_transport_or_null(config):
    """Email transport is only useful with SMTP credentials. In dev/test,
    fall back to NullTransport so the system still functions for non-email
    subscribers."""
    import os
    smtp_host = os.environ.get("TRIP_EMA_SMTP_HOST")
    if not smtp_host:
        logger.info(
            "TRIP_EMA_SMTP_HOST not set — using NullTransport for email"
        )
        return NullTransport()
    return EmailTransport(
        from_address=os.environ.get(
            "TRIP_EMA_EMAIL_FROM", "alerts@trip-ema.local",
        ),
        smtp_host=smtp_host,
        smtp_port=int(os.environ.get("TRIP_EMA_SMTP_PORT", "587")),
        smtp_user=os.environ.get("TRIP_EMA_SMTP_USER"),
        smtp_password=os.environ.get("TRIP_EMA_SMTP_PASSWORD"),
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
        try:
            if state.distributor is not None:
                state.distributor.stop()
        except Exception:
            logger.exception("Error stopping distributor")
        try:
            state.compute_scheduler.stop()
        except Exception:
            logger.exception("Error stopping compute_scheduler")
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
        try:
            state.db_engine.dispose()
        except Exception:
            logger.exception("Error disposing db_engine")
        logger.info("Lifespan shutdown complete")
    return _shutdown


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


def _spawn_notifier_process(config_path: Path, log_level: str):
    """Spawn the notifier worker as a child process. Returns the
    multiprocessing.Process so the caller can manage its lifecycle.

    The child re-loads the same config file and connects to the same
    Redis. In production, prefer running the notifier as a separate
    systemd unit (`python -m trip_ema.notifier_worker --config ...`);
    this in-process spawn is the dev convenience path.
    """
    import multiprocessing

    def _worker_entry(cfg_path: str, lvl: str):
        # The child process re-imports and re-parses — no state shared.
        from .notifier_worker import main as worker_main
        worker_main(["--config", cfg_path, "--log-level", lvl])

    proc = multiprocessing.Process(
        target=_worker_entry,
        args=(str(config_path), log_level),
        name="trip_ema_notifier",
        daemon=False,  # daemon=True would kill it abruptly on parent exit
    )
    proc.start()
    logger.info("Spawned notifier worker process (pid=%d)", proc.pid)
    return proc


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
    parser.add_argument(
        "--spawn-notifier", action="store_true",
        help="Spawn the notifier worker as a child process. Convenient "
             "for dev; in production, run the notifier separately via "
             "`python -m trip_ema.notifier_worker --config ...` so it "
             "can be managed and scaled independently of the main app.",
    )
    args = parser.parse_args(argv)

    # Bootstrap logging in text mode so the early boot lines are
    # readable. After the config loads we'll re-configure based on the
    # user's preference (log_json), which is idempotent.
    _configure_logging(args.log_level, json_output=False)
    state = _build_app_state(args.config)
    # Apply the user's preferred log format (json vs text). The
    # observability.configure_logging is idempotent — clears existing
    # handlers — so this safely replaces the bootstrap handler.
    _configure_logging(args.log_level, json_output=state.config.observability.log_json)
    set_state(state)

    notifier_proc = None
    if args.spawn_notifier:
        if state.redis_queue is None:
            logger.error(
                "--spawn-notifier requires a `redis` section in the config"
            )
            sys.exit(2)
        notifier_proc = _spawn_notifier_process(args.config, args.log_level)

    # Build the graceful-shutdown callback ONCE; share it between the
    # ASGI lifespan (the path uvicorn uses on SIGTERM/SIGINT) and the
    # explicit signal handlers (the fallback for non-uvicorn paths).
    shutdown_callback = _build_shutdown_callback(state, notifier_proc=notifier_proc)
    _install_signal_handlers(shutdown_callback)

    app = create_app(shutdown_callback=shutdown_callback)

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
