# `trip_ema/observability/` — metrics, tracing, correlation, logging

Cross-cutting plumbing for operations: Prometheus metrics, optional
OpenTelemetry tracing, correlation-ID threading through the pipeline,
and structured logging.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports `REGISTRY`, `configure_logging`, `correlation_id_var`, the public Prometheus counter / histogram factories |
| `metrics.py` | Central Prometheus registry. All metrics on a single `CollectorRegistry` exposed as `REGISTRY`. Naming: `trip_ema_<subsystem>_<unit>_<total\|seconds>` for counters / durations |
| `metrics_server.py` | Standalone Prometheus scrape endpoint on a separate port (typically 9090). Three reasons separate: firewall the scrape port, disable in dev without touching the main API, survives mid-shutdown of the main app |
| `tracing.py` | OpenTelemetry — optional, off-by-default. Every span site calls `tracer.start_as_current_span(...)`. No-op tracer if no provider is initialised → near-zero cost when disabled, real spans when an exporter is wired up |
| `correlation.py` | UUID4 correlation IDs threaded through scheduler → cache write → distributor → Redis → worker → transport. Grep-ability across log streams. Stored in a `contextvars.ContextVar` so it survives async/thread hand-offs |
| `logging_config.py` | `configure_logging(level, json_output)`. JSON to stderr (production) or plain text (dev). Always includes the current `correlation_id` |

## Boundaries

- **No business logic.** Every module here is plumbing.
- Importable from anywhere in `trip_ema`. Has no dependency on
  `persistence`, `transports/http`, etc. — they depend on this.
- Metric and span names are public-API: changing them breaks
  dashboards and alerts.

## Why correlation ≠ tracing

OpenTelemetry tracing answers "what was the call graph and where did
time go." Correlation IDs answer "tell me everything that happened
for this one user-visible operation." They're complementary; a single
correlation ID will appear on many spans in a trace.

## Operator quick reference

```
Logs:          /tmp/trip-ema.log
Metrics:       http://<host>:9090/metrics
JSON logs:     export TRIP_EMA_JSON_LOGS=1 before service start
OTel exporter: configured via standard OTEL_* env vars
```
