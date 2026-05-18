"""Prometheus metrics — central registry.

All metrics are registered on a single `CollectorRegistry` exposed
as `REGISTRY`. The metrics server (see `metrics_server.py`) scrapes
this registry.

Naming follows Prometheus conventions:
- `trip_ema_<subsystem>_<unit>_<total|seconds>` for counters/durations.
- All names use snake_case, no dashes.
- Labels are kept small in cardinality — never include free-form
  ticker symbols in labels unless the ticker count is bounded and
  small. (We DO use ticker labels in a few places where they're
  legitimately useful; cardinality is bounded by registered tickers
  in the system, typically <= a few hundred.)

The metrics defined here are the ones the production code uses to
record signal/scheduler/notification activity. Tests don't reset
this registry between runs by default; if a test needs isolation,
use `prometheus_client.CollectorRegistry()` for that test and pass
it to a custom instance.

Optional-dependency handling: if `prometheus_client` is not installed,
this module's symbols are bound to small no-op classes so the rest of
the codebase keeps importing cleanly. The instrumentation call sites
(`M.scheduler_runs_total.labels(...).inc()`) continue to work; they
simply do nothing. This mirrors the OpenTelemetry pattern in
`tracing.py` and the broader contract that observability is additive
and never load-bearing (ARCHITECTURE.md invariant 3.12).
"""

from __future__ import annotations

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False

    class _NoOpMetric:
        """Stand-in for Counter/Gauge/Histogram when prometheus_client
        isn't installed. Every method is a no-op; labels(...) returns
        self so chained calls compile cleanly."""

        def __init__(self, *args, **kwargs): pass
        def labels(self, *args, **kwargs): return self
        def inc(self, *args, **kwargs): pass
        def dec(self, *args, **kwargs): pass
        def set(self, *args, **kwargs): pass
        def observe(self, *args, **kwargs): pass

        class _NoOpTimer:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __call__(self, fn): return fn

        def time(self): return self._NoOpTimer()

        @property
        def _value(self):
            class _V:
                @staticmethod
                def get(): return 0.0
                @staticmethod
                def set(v): pass
            return _V()

    class _NoOpRegistry:
        """No-op stand-in for CollectorRegistry."""
        def __init__(self, *a, **kw):
            self._names_to_collectors = {}

    Counter = Gauge = Histogram = _NoOpMetric
    CollectorRegistry = _NoOpRegistry


# Single shared registry. The metrics_server scrapes this.
REGISTRY = CollectorRegistry()


# ---------- Scheduler ---------- #

scheduler_runs_total = Counter(
    "trip_ema_scheduler_runs_total",
    "Number of ComputeScheduler.run_once() invocations.",
    labelnames=("outcome",),  # "completed" | "skipped_concurrent"
    registry=REGISTRY,
)

scheduler_run_duration_seconds = Histogram(
    "trip_ema_scheduler_run_duration_seconds",
    "Wall-clock duration of ComputeScheduler.run_once() invocations.",
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 300.0),
    registry=REGISTRY,
)

scheduler_tickers_examined_total = Counter(
    "trip_ema_scheduler_tickers_examined_total",
    "Tickers examined across all run_once() invocations.",
    registry=REGISTRY,
)

scheduler_bars_ingested_total = Counter(
    "trip_ema_scheduler_bars_ingested_total",
    "Bars fed into engine.ingest() across all run_once() invocations.",
    registry=REGISTRY,
)

scheduler_signal_events_total = Counter(
    "trip_ema_scheduler_signal_events_total",
    "SignalEvent rows persisted (regime changes detected).",
    labelnames=("signal",),  # ChangeSignal name: NO_CHANGE, BUY, EXIT, ...
    registry=REGISTRY,
)

scheduler_errors_total = Counter(
    "trip_ema_scheduler_errors_total",
    "Per-ticker exceptions caught during run_once().",
    registry=REGISTRY,
)

scheduler_stale_tickers = Gauge(
    "trip_ema_scheduler_stale_tickers",
    "Current count of tickers flagged stale (data-gap detected).",
    registry=REGISTRY,
)

scheduler_tickers_reprimed_total = Counter(
    "trip_ema_scheduler_tickers_reprimed_total",
    "Tickers re-primed due to historical revision detection.",
    registry=REGISTRY,
)


# ---------- Subscriber distributor ---------- #

distributor_runs_total = Counter(
    "trip_ema_distributor_runs_total",
    "SubscriberDistributor.run_once() invocations.",
    labelnames=("outcome",),  # "completed" | "skipped_market_closed"
    registry=REGISTRY,
)

distributor_jobs_enqueued_total = Counter(
    "trip_ema_distributor_jobs_enqueued_total",
    "Notification jobs enqueued to Redis (queue mode) or dispatched "
    "in-process (legacy mode).",
    labelnames=("transport",),
    registry=REGISTRY,
)


# ---------- Notifier worker ---------- #

notifier_jobs_processed_total = Counter(
    "trip_ema_notifier_jobs_processed_total",
    "Jobs popped from the Redis queue.",
    labelnames=("outcome", "transport"),
    # outcome: "sent" | "superseded" | "failed"
    registry=REGISTRY,
)

notifier_dispatch_duration_seconds = Histogram(
    "trip_ema_notifier_dispatch_duration_seconds",
    "Wall-clock duration of NotifierWorker._dispatch_and_log per job.",
    labelnames=("transport",),
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)


# ---------- Cache & registry ---------- #

cache_size = Gauge(
    "trip_ema_cache_size",
    "Number of entries currently in the SignalCache.",
    registry=REGISTRY,
)

registry_size = Gauge(
    "trip_ema_registry_size",
    "Number of (ticker, timeframe) engines in the TickerRegistry.",
    registry=REGISTRY,
)


# ---------- Redis queue ---------- #

redis_queue_depth = Gauge(
    "trip_ema_redis_queue_depth",
    "Current depth of the notification Redis queue.",
    registry=REGISTRY,
)


# ---------- Build / version info ---------- #

# Convention: a Gauge always set to 1.0, with labels carrying static
# information about the running process. Dashboards correlate
# "this regression started here" with the deployed version by joining
# any production metric against trip_ema_info on the same scrape.
trip_ema_info = Gauge(
    "trip_ema_info",
    "Static information about the running process. Value is always 1; "
    "the labels carry the data.",
    labelnames=("version",),
    registry=REGISTRY,
)


def set_build_info(version: str) -> None:
    """Set the trip_ema_info gauge to record the current version. Call
    once at startup from app.py after the config has loaded.

    Idempotent — subsequent calls with the same version are no-ops;
    subsequent calls with a different version (e.g., during a test
    that simulates a rolling restart) overwrite cleanly.
    """
    trip_ema_info.labels(version=version).set(1.0)


def reset_metrics_for_test() -> None:
    """Test helper — clears all counter/gauge/histogram values without
    unregistering them. Call from a fixture in tests that assert metric
    values."""
    for collector in list(REGISTRY._names_to_collectors.values()):
        if hasattr(collector, "_metrics"):
            collector._metrics.clear()
        if hasattr(collector, "_value") and hasattr(collector._value, "set"):
            try:
                collector._value.set(0)
            except Exception:
                pass
