# `trip_ema/observability/` — Cross-cutting observability stack

Logs, metrics, tracing, and correlation-ID propagation. Consumed by
every layer; depends on no other in-package layer (except `domain/`
when needed).

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `logging_config.py` | Structured JSON log formatter, log-level / handler setup. Switched on with `observability.log_json: true` in config |
| `metrics.py` | Prometheus metric definitions — counters, histograms, gauges. Defensive no-op shims (`_NoOpMetric`, `_NoOpRegistry`) when `prometheus_client` isn't installed |
| `metrics_server.py` | Starts the `/metrics` HTTP server on a separate port (`observability.metrics_port`) — only when `prometheus_client` is installed |
| `tracing.py` | OpenTelemetry tracer factory. Defensive `_NoOpTracer` shim when `opentelemetry-api` isn't installed. Spans wrap `ComputeScheduler.run_once`, `SubscriberDistributor._enqueue_jobs`, `NotifierWorker._handle_job`, `generate_historical_signals` |
| `correlation.py` | Correlation-ID context-var — minted at HTTP request boundary, tick boundary, distributor enqueue, notifier handle, backtest run start. Propagates through every log line, every DB row that has a `correlation_id` column, and every OTel span |

## Invariant 3.12 — observability is additive

A Tier-3 deployment without `prometheus_client` or
`opentelemetry-api` installed must keep working. Achieved by:
- All imports in this directory are guarded by `try/except ImportError`
- Defensive no-op shims replace the real classes when imports fail
- The rest of the codebase imports the real or shim symbol from
  here — the call sites never know the difference

This is why `prometheus_client` and `opentelemetry-*` are **extras**
in `pyproject.toml` rather than runtime dependencies.

## Where correlation IDs are minted

| Boundary | Minter |
|---|---|
| HTTP request | Middleware reads `X-Correlation-Id` header, or mints UUID |
| Compute tick | `ComputeScheduler.run_once()` |
| Distributor enqueue | `SubscriberDistributor._enqueue_jobs` |
| Notifier dispatch | `NotifierWorker._handle_job` |
| Backtest run | `generate_historical_signals` |

## Logs / metrics / traces

- **Logs** — JSON to stdout (default) or plain text. Every log line
  carries the current correlation ID (when in context)
- **Metrics** — Prometheus scrape on `observability.metrics_port`.
  Key metrics: `trip_ema_compute_run_duration_seconds`,
  `trip_ema_signals_emitted_total`,
  `trip_ema_notifier_jobs_processed_total` (label `outcome=success|failed|dlq`),
  `trip_ema_circuit_breaker_state` (gauge per transport)
- **Traces** — OTLP / console exporter. Spans labeled with
  correlation ID + tick boundary

## Tests

- `tests/observability/test_observability.py` — basic plumbing
- `tests/observability/test_metrics_integration.py` — metric
  registration + emission
- `tests/observability/test_tracing.py` — span lifecycle, no-op
  shim behavior
