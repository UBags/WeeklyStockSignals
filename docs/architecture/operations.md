# Deployment and operations

See ARCHITECTURE.md §8 for the full reference. Highlights:

- **§8.1** — Single-writer, single-process by design (why we don't
  run multiple engine replicas)
- **§8.2** — Graceful-shutdown timing (the 10-second budget)
- **§8.3** — Prometheus scrape configuration
- **§8.4** — What `stale_tickers_count` actually means
- **§8.5** — Pine parity harness workflow
- **§8.6** — DB role separation (Phase 12)

## Process inventory

| Process | What it does | DB role | Restartable? |
|---|---|---|---|
| `trip-ema` | Main service — engine + HTTP API | `trip_engine_user` | Yes (state rebuilds from bars) |
| `trip-ema-etl` | ETL CLI — refresh listings, EoD, backfill | `trip_etl_user` | Yes (idempotent) |
| `trip-ema-notifier` | Worker — dispatches Redis jobs to transports | `trip_engine_user` | Yes (DLQ catches misses) |
| `trip-ema-console` | PySide6 operator console | `trip_gui_user` | Trivially (no state) |

Migrations run as a higher-privilege user (the top-level
`database.user` in config.yaml), not as any per-role user.

## Observability surface

- **`/healthz`** — liveness; returns 200 if the process is up
- **`/readyz`** — readiness; returns 200 if the cache is primed
  AND the latest scheduler tick was recent
- **`/metrics`** (on a separate port) — Prometheus exposition
- **OpenTelemetry traces** — exported via OTLP when configured;
  `console` exporter for local development
