# `trip_ema/observability/` — metrics, tracing, correlation, logging

## 1. Role in the larger application

`observability/` is the **cross-cutting plumbing** every other layer
calls into to make itself diagnosable in production. Five
responsibilities:

1. **Structured logging** with JSON output for production log
   aggregation and plain text for dev (`logging_config.py`).
2. **Correlation IDs** threaded through scheduler → cache write
   → distributor → Redis → worker → transport, so an operator can
   grep one ID across log streams and see the full lifecycle of a
   single logical operation (`correlation.py`).
3. **Prometheus metrics** on a single shared `CollectorRegistry`
   covering scheduler, distributor, notifier, ETL, cache, redis,
   and build-info (`metrics.py`).
4. **Standalone metrics scrape endpoint** on a separate port so
   metrics are firewallable and survive main-app shutdown
   (`metrics_server.py`).
5. **OpenTelemetry tracing** — optional, off-by-default, with a
   safe no-op fallback so call sites can be unconditional
   (`tracing.py`).

```mermaid
flowchart TB
    code[Every other trip_ema package] --> log[logger.info / .error]
    code --> met[M.counter.labels(...).inc()]
    code --> span[get_tracer().start_as_current_span(...)]
    code --> cid[correlation_id(uuid)]

    log --> handler[StreamHandler -> stderr]
    handler -.JSON / text.-> capture[/tmp/trip-ema.log]
    cid -.contextvar.-> handler

    met --> reg[REGISTRY: CollectorRegistry]
    reg --> srv[MetricsServer :9090]
    srv -.scrape.-> prom[Prometheus]

    span --> otel[OTel SDK if configured]
    otel -.OTLP.-> collector[OTel Collector]
```

## 2. Architecture and constraints

| Rule | Why |
|------|-----|
| **No business logic.** Every module here is plumbing | An observability outage must not break the service |
| **Importable from anywhere.** No dependency on `persistence`, `transports/http`, etc. | This is the bottom of the dependency graph alongside `domain` |
| **Additive, never load-bearing.** If `prometheus_client` or `opentelemetry` isn't installed, no-op fallbacks keep call sites importing cleanly | ARCHITECTURE.md invariant 3.12 |
| **Metric and span names are public-API.** Changing them breaks dashboards and alerts | Treat renames like API changes |
| **Cardinality discipline.** Don't put unbounded values (ticker symbols on a service with 10K tickers, raw error strings) in labels | Cardinality explosion is the #1 way to break a Prometheus deployment |

## 3. File inventory

```
trip_ema/observability/
├── __init__.py            Public surface re-exports
├── correlation.py         Correlation-ID contextvar + helpers
├── logging_config.py      configure_logging(), JsonFormatter, TextFormatter
├── metrics.py             REGISTRY + every counter/gauge/histogram + reset
├── metrics_server.py      MetricsServer + start_metrics_server
└── tracing.py             configure_tracing + get_tracer + no-op fallback
```

## 4. `correlation.py` — per-operation IDs

### 4.1 What problem this solves

OpenTelemetry tracing answers "what was the call graph; where did
time go?" Correlation IDs answer **"tell me everything that
happened for this one user-visible operation."** They're
complementary; a single correlation ID will appear on many spans
in a trace and across many log lines.

Concretely, the scheduler tick at 09:30 IST:

1. Mints a correlation ID at the start of the tick.
2. Pushes it onto a `contextvars.ContextVar`.
3. Every subsequent log line (in the scheduler, in the orchestrator
   pipeline, in the distributor, in the worker if queue-mode, in
   the Brevo HTTP transport) picks the ID up from the contextvar
   and includes it in the JSON record.
4. An operator with `grep correlation_id=abc-123 /tmp/trip-ema.log`
   sees every event that participated in that tick.

### 4.2 API

```python
def current_correlation_id() -> str | None
def new_correlation_id() -> str                  # mint a fresh UUID4
def get_or_mint_correlation_id() -> str          # return current, or mint without pushing

@contextmanager
def correlation_id(cid: str | None) -> Iterator[str | None]:
    """Push `cid` onto the context for the duration of the with-block."""
```

`correlation_id` is re-entrant: nested calls save the prior value
and restore it on exit. Passing `None` explicitly clears the ID
inside the block (useful when crossing a boundary into code that
shouldn't be correlated with the outer logical operation).

### 4.3 Why `contextvars`, not `threading.local`?

`contextvars` propagate correctly under both `threading` and
`asyncio`. `threading.local` doesn't propagate into asyncio tasks —
the same coroutine resumed on a different thread would lose the ID.
The codebase uses both (HTTP layer is async, scheduler is
threaded), so `contextvars` is the only correct choice.

### 4.4 Cross-process propagation

For Redis-queued jobs, the producer reads
`get_or_mint_correlation_id()` and **stores the ID in the job
payload**. The consumer (notifier worker) reads it back and pushes
it onto its own context via `with correlation_id(payload['cid'])`.
That way the worker's log lines carry the same ID as the
distributor's.

V1's in-process notification path doesn't need this — the ID
propagates naturally through the contextvar.

### 4.5 Wire-up at boundaries

```python
# At a logical-operation boundary (e.g. a scheduler tick):
with correlation_id(new_correlation_id()):
    run_tick(...)
    # every log inside picks up the cid
```

```python
# At a request boundary (HTTP route):
@app.middleware("http")
async def correlation_middleware(request, call_next):
    cid = request.headers.get("X-Correlation-Id") or new_correlation_id()
    with correlation_id(cid):
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        return response
```

## 5. `logging_config.py` — structured logging

### 5.1 `configure_logging(level, json_output)`

```python
def configure_logging(
    level:       int | str = "INFO",
    json_output: bool      = False,
) -> None:
```

Replaces the root logger's handlers with a single `StreamHandler` to
stderr using one of two formatters:

- `JsonFormatter` — one JSON object per line, picks up
  `correlation_id` automatically. Production.
- `TextFormatter` — human-friendly format, includes a short
  `[cid=abc-1234]` suffix when correlation is active. Dev.

**Idempotent in two senses:**

1. Repeated calls with the same args are no-ops.
2. Re-calls with different `json_output` swap the formatter
   **in place** on the existing handler rather than tearing down
   and rebuilding. This matters for `app.py`'s two-phase
   bootstrap: an early text-mode `configure_logging` call before
   config is loaded, then a re-call once `config.observability.log_json`
   is known. The in-place swap ensures no records emitted between
   the two calls are lost.

### 5.2 The JSON record shape

```json
{
  "ts":             "2026-06-29T15:00:00.123456+00:00",
  "level":          "INFO",
  "logger":         "trip_ema.orchestrator.recompute_market",
  "message":        "Cycle complete in 4.2s",
  "correlation_id": "0bb88c25-3a4b-4a08-8b14-7f6e0c0e88e3",
  "ticker":         "NSE:BALAMINES",
  "regime":         "BULLISH"
}
```

The first four fields are always present. `correlation_id` is
included when active. Anything else passed via `extra={...}` to the
log call appears as a top-level field.

**Coercion rule.** Non-JSON-serializable `extra` values are coerced
to `str()` rather than blowing up the log call. `extra={"err":
SomeException(...)}` will appear as a string repr; `extra={"date":
datetime.now()}` will appear via `default=str` (ISO format).

### 5.3 `_reset_for_tests()`

```python
def _reset_for_tests() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
```

Test fixtures call this when they need a known-clean logger state
between tests. Not for production use.

## 6. `metrics.py` — the Prometheus registry

### 6.1 The shared `REGISTRY`

```python
REGISTRY = CollectorRegistry()
```

One process-wide registry. Every metric is registered on this. The
`MetricsServer` scrapes only this. Tests can construct their own
`CollectorRegistry()` for isolation, but the production path always
uses this one.

### 6.2 Optional-dependency handling

```python
try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False

    class _NoOpMetric:
        def labels(self, *a, **kw):  return self
        def inc(self, *a, **kw):     pass
        def set(self, *a, **kw):     pass
        def observe(self, *a, **kw): pass
        def time(self):              return _NoOpMetric._NoOpTimer()
        # ...

    Counter = Gauge = Histogram = _NoOpMetric
    CollectorRegistry = _NoOpRegistry
```

When `prometheus_client` isn't installed, every metric becomes a
no-op shim with the same method shape. Call sites like
`M.scheduler_runs_total.labels(outcome="completed").inc()` continue
to work; they simply do nothing.

This mirrors the OTel pattern in `tracing.py` and the broader
contract: **observability is additive and never load-bearing**.

### 6.3 Metric inventory

**Naming convention:** `trip_ema_<subsystem>_<unit>_<total|seconds>`.
Counters end in `_total`; histograms measuring duration end in
`_seconds`; gauges have no suffix.

| Subsystem | Metric | Type | Labels | Meaning |
|-----------|--------|------|--------|---------|
| Scheduler | `scheduler_runs_total` | Counter | `outcome` | Number of `run_once()` invocations; outcome is `completed` or `skipped_concurrent` |
| Scheduler | `scheduler_run_duration_seconds` | Histogram | — | Wall-clock duration; buckets cover 10ms to 5 min |
| Scheduler | `scheduler_tickers_examined_total` | Counter | — | Tickers walked per run |
| Scheduler | `scheduler_bars_ingested_total` | Counter | — | Bars fed into engines per run |
| Scheduler | `scheduler_signal_events_total` | Counter | `signal` | Signal rows persisted; signal is `BUY` / `SELL` / `EXIT_FROM_*` / `NO_CHANGE` |
| Scheduler | `scheduler_errors_total` | Counter | — | Per-ticker exceptions caught |
| Scheduler | `scheduler_stale_tickers` | Gauge | — | Current count of stale-flagged tickers |
| Scheduler | `scheduler_tickers_reprimed_total` | Counter | — | Re-primed due to historical revision |
| Distributor | `distributor_runs_total` | Counter | `outcome` | `run_once` calls; outcome is `completed` or `skipped_market_closed` |
| Distributor | `distributor_jobs_enqueued_total` | Counter | `transport` | Jobs enqueued (Redis) or dispatched (legacy in-process) |
| Notifier worker | `notifier_jobs_processed_total` | Counter | `outcome, transport` | outcome: `sent` / `superseded` / `failed` / `dlq` |
| Notifier worker | `notifier_dispatch_duration_seconds` | Histogram | `transport` | Per-job dispatch duration |
| Cache | `cache_size` | Gauge | — | Current `SignalCache` entry count |
| Cache | `registry_size` | Gauge | — | TickerRegistry engine count (legacy) |
| ETL | `etl_runs_total` | Counter | `kind, outcome` | Per-subcommand outcomes; `kind` is one of `refresh-listings`, `backfill`, `eod`, `intraday`, `tier-recompute`, `unpark`, `approve-action` |
| ETL | `etl_tickers_fetched_total` | Counter | `tier, outcome` | Tickers attempted / succeeded / failed by tier |
| ETL | `etl_bars_written_total` | Counter | — | Bars upserted into `ohlcv_bars` |
| ETL | `etl_run_duration_seconds` | Histogram | `kind` | Per-kind run duration; buckets to 3 hours |
| ETL | `etl_parked_tickers` | Gauge | — | Currently parked tickers |
| ETL | `etl_divergences_flagged_total` | Counter | — | Historical-bar divergences awaiting review |
| Redis | `redis_queue_depth` | Gauge | — | Current notification queue depth |
| Process | `trip_ema_info` | Gauge | `version` | Always 1; labels carry version data |

### 6.4 Cardinality discipline

Labels are kept small. No metric labels include free-form ticker
symbols — that would balloon cardinality to the universe size
(~2,667 for NSE alone, multiplied across exchanges). The
`scheduler_signal_events_total{signal="BUY"}` shape is fine
because `signal` is one of 5 fixed enum values.

If per-ticker visibility is genuinely needed, the right answer is
to query the DB, not the metrics registry.

### 6.5 Build-info pattern

```python
trip_ema_info = Gauge(
    "trip_ema_info", "...", labelnames=("version",), registry=REGISTRY,
)

def set_build_info(version: str) -> None:
    trip_ema_info.labels(version=version).set(1.0)
```

The "info" pattern: a gauge always set to 1.0 with static info in
labels. Dashboards join production metrics against
`trip_ema_info` on the same scrape to correlate regressions with
deployed versions.

### 6.6 `reset_metrics_for_test()`

Test helper that clears counter / gauge / histogram values without
unregistering them. Uses prom_client's private attributes
deliberately — the supported reset path is to construct a fresh
registry, but tests want to assert against the global one.

## 7. `metrics_server.py` — the scrape endpoint

### 7.1 Why a separate port

Three reasons the metrics server lives on its own port (typically
9090) instead of mounting `/metrics` on the main FastAPI app:

1. **Firewall separately** — operators can expose the scrape port
   only to Prometheus, not to public clients.
2. **Disable in dev** — set `observability.metrics_port: null` in
   config; the server isn't started; main API is unaffected.
3. **Survives main-app shutdown** — if the main API stops responding
   for some reason, the metrics server keeps reporting (so the
   scrape that detects the outage isn't itself broken).

### 7.2 `start_metrics_server(port) -> MetricsServer | None`

```python
def start_metrics_server(port: int | None) -> MetricsServer | None:
    if port is None:
        logger.info("Metrics server disabled (port=None)")
        return None
    try:
        from prometheus_client import start_http_server
    except ImportError:
        logger.info("prometheus_client not installed; metrics server disabled")
        return None
    try:
        http_server, thread = start_http_server(port, registry=REGISTRY)
        logger.info("Metrics server started on port %d", port)
        return MetricsServer(http_server, thread, port)
    except Exception:
        logger.exception("Failed to start metrics server; continuing without metrics")
        return None
```

**Three failure modes, all soft:**

1. `port is None` → disabled-by-config; INFO log; `None` returned.
2. `prometheus_client` missing → disabled-by-import; INFO log;
   `None` returned.
3. Bind failure (port in use, permission denied) → exception
   logged, function returns `None`. **The main service continues
   to start.**

The third behaviour is intentional: metrics failure must never
prevent the main service from starting. Better to run without
metrics than not run at all.

### 7.3 `MetricsServer.stop(timeout=5)`

Closes the HTTP server and joins the thread. Each step is wrapped
in try/except so cleanup failures don't propagate; only logged.

## 8. `tracing.py` — OpenTelemetry

### 8.1 Defensive triple-fallback

Three layers of fallback ensure call sites are always safe:

1. **OTel installed + provider configured** — real spans, real
   exporter.
2. **OTel installed + no provider configured** — OTel's own
   no-op tracer (near-zero cost; `is_recording() = False`).
3. **OTel not installed** — local `_NoOpTracer` / `_NoOpSpan`
   classes here.

Either way, `get_tracer().start_as_current_span("name")` is safe
and call-site code never branches on tracer availability.

### 8.2 `configure_tracing(service_name, exporter, otlp_endpoint)`

```python
def configure_tracing(
    service_name:   str = "trip_ema",
    exporter:       str = "console",
    otlp_endpoint:  str | None = None,
) -> None:
```

Set up the global TracerProvider. Two exporters:

- `console` (dev) — prints spans to stderr.
- `otlp` (production) — sends to an OTLP collector. `otlp_endpoint`
  required (e.g. `http://otel-collector:4317`).

**Failure handling:**

- OTel not installed → INFO log, return; the no-op tracer continues
  to be used.
- OTLP exporter not installed but `exporter='otlp'` → ERROR log,
  return.
- Unknown exporter name → ERROR log, return.
- Any other exception → `logger.exception(...)`, return.

In every failure path, the function returns normally. The service
continues without tracing.

### 8.3 `get_tracer(name) -> Tracer`

Always-safe constructor. Always returns something with a
`start_as_current_span` context-manager method.

### 8.4 `add_correlation_id_to_span()`

```python
def add_correlation_id_to_span() -> None:
    cid = current_correlation_id()
    if cid is not None:
        set_span_attribute_if_active("correlation_id", cid)
```

Bridges the two systems. Call once near the start of a function
with both a correlation_id context and a span — the pairing lets an
operator cross-reference logs and traces:

```
log line:  "computed signal"  correlation_id=abc-123
trace span: "compute_for_ticker"  correlation_id=abc-123, trace_id=...
```

### 8.5 `set_span_attribute_if_active(key, value)`

Sets an attribute on the **currently active** span, if any. Safe to
call when no span is active or OTel isn't installed. Use to enrich
spans with domain context (ticker, timeframe, etc.) without forcing
the caller to know about tracer state.

## 9. Wiring at startup (`app.py`)

```python
def build_app():
    # Stage 1: bootstrap text logging (no config yet)
    configure_logging(level="INFO", json_output=False)

    # Load config
    config = load_config(...)

    # Stage 2: reconfigure with operator preferences
    configure_logging(
        level=config.observability.log_level,
        json_output=config.observability.log_json,
    )

    # Tracing — opt-in
    if config.observability.tracing.enabled:
        configure_tracing(
            service_name=config.observability.tracing.service_name,
            exporter=config.observability.tracing.exporter,
            otlp_endpoint=config.observability.tracing.otlp_endpoint,
        )

    # Metrics server — opt-in
    metrics_server = start_metrics_server(config.observability.metrics_port)

    # Set build-info gauge
    from trip_ema import __version__
    set_build_info(__version__)

    # ... rest of startup
```

The stage-1/stage-2 logging dance is the reason `configure_logging`
swaps formatters in place. Records emitted between the two calls
(by `load_config` itself, etc.) survive.

## 10. Operator quick reference

| Knob | Where | What |
|------|-------|------|
| Plain text logs | `config.yaml: observability.log_json: false` | Default. Human-readable to stderr |
| JSON logs | `observability.log_json: true` | For log aggregators (Loki / ELK / Datadog) |
| Log level | `observability.log_level: INFO` | Standard Python levels |
| Metrics port | `observability.metrics_port: 9090` (or `null`) | Separate scrape endpoint |
| OTel console | `observability.tracing.exporter: console` | Dev — spans to stderr |
| OTel OTLP | `observability.tracing.exporter: otlp` + endpoint | Production tracing |

Production logs land in `/tmp/trip-ema.log` (the `nohup` redirect
target — see `restart_service.sh`).

## 11. Common pitfalls

1. **Renaming a metric.** It's a public-API change. Update
   dashboards and alerts in lockstep.
2. **Adding a high-cardinality label.** Don't. The metrics endpoint
   will balloon; Prometheus scrape will get slow; queries will
   timeout. Use a structured log with `extra=` instead.
3. **Calling `configure_logging` from inside a test without
   `_reset_for_tests()`.** Handlers accumulate; output gets
   duplicated.
4. **Correlation IDs longer than 8 characters in dev text logs.**
   The text formatter shows only `[cid=abc-1234]` (first 8) for
   readability. The full ID is in the underlying record; switch to
   JSON if you need the full ID inline.
5. **Calling `configure_tracing` more than once.** Idempotent in the
   "no error" sense, but the second call replaces the provider —
   any in-flight spans on the previous provider may not export.
6. **Treating metrics as the source of truth.** Counters can be lost
   on a restart; gauges drift if writers skip a beat. Metrics are a
   trend tool, not an audit log.

## 12. Testing

`tests/observability/`:

| File | Coverage |
|------|----------|
| `test_correlation.py` | Push/pop, nested contexts, `None` clearing, contextvar propagation across `Thread.start()` |
| `test_logging_config.py` | JSON shape, text format, idempotent re-call swap-in-place, `extra=` field coercion |
| `test_metrics.py` | Metric counter increments + gauge sets, no-op shim path (when prom_client is stubbed out), `reset_metrics_for_test` correctness |
| `test_metrics_server.py` | Three soft-fail paths (port=None, missing library, bind failure) |
| `test_tracing.py` | No-op fallback when OTel not installed, configure idempotency |

## 13. Extension paths

- **A new metric.** Add it in `metrics.py` next to its
  subsystem peers. Document it in this README's inventory table.
  Dashboards/alerts get updated separately.
- **A new logger field.** Use `logger.info(msg, extra={"new_field":
  value})` at the call site. The formatter spreads it
  automatically.
- **A new exporter.** Add a case in `configure_tracing` for the new
  exporter type. Keep the soft-fail behaviour.

## 14. Related packages

- Used by **every** other `trip_ema.*` package. Every module that
  has a logger has `import logging; logger = logging.getLogger(__name__)`.
- `trip_ema/transports/http/middleware.py` — push a correlation ID
  per request, propagate to response headers.
- `trip_ema/orchestrator/recompute_market.py` — mints a correlation
  ID per cycle and records timing on `scheduler_run_duration_seconds`.
- `trip_ema/etl/cli.py` — emits `etl_runs_total`, `etl_run_duration_seconds`
  on every subcommand invocation.
- `trip_ema/notifications/email_transport.py` — emits
  `notifier_dispatch_duration_seconds`; calls `add_correlation_id_to_span`
  on the dispatch span.
