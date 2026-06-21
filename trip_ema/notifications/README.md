# `trip_ema/notifications/` — delivery pipeline + benchmark stats

The pieces between an emitted `SignalEvent` and a delivered message
in a subscriber's inbox. Composition, enrichment, transports, the
Redis-backed queue, circuit breakers, and the Phase 6-3 benchmark
stats that re-rank BUY signals before they ship.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports the transport interface + concrete transports + queue + circuit breaker |
| `base.py` | `NotificationTransport` Protocol. `(address, NotificationBatch) -> bool`. True = delivered, False = retryable failure. Raises only on unrecoverable bugs |
| `batcher.py` | `NotificationBatch` → transport-ready payloads. Three renderings: **text** (one event per line, used everywhere as text/plain), **html** (rich, colour-coded, grouped-by-signal-type, built around `enrichment`), and a webhook JSON shape |
| `email_transport.py` | Brevo HTTP API transport (`POST /v3/smtp/email`). Replaces SMTP. Microsoft + Google have progressively disabled SMTP AUTH for personal mailboxes; HTTP API is the only reliable path. Verified sender: `weeklystocksignals@outlook.com`. API key from `secrets/brevo.yaml` via `config.brevo_secrets` |
| `webhook_transport.py` | POSTs the JSON payload to the subscriber's URL. HTTP poster is injectable for tests (production: `requests`) |
| `redis_queue.py` | Tiny Redis-backed work queue with atomic dedup. Two structures: a LIST (FIFO of pending jobs, LPUSH/BRPOP) and a HASH (job_id → enqueue timestamp for dedup) |
| `circuit_breaker.py` | `CircuitBreakingTransport` wraps any `NotificationTransport` with a CLOSED → OPEN → HALF_OPEN state machine. Fast-fails calls when the underlying transport is sufficiently broken |
| `enrichment.py` | Per-batch enrichment for HTML email. Looks up market cap, sector, sector's current display regime, day-over-day sector direction, and last-5 signals per ticker. Data is spread across `instrument_industry`, `daily_market_cap`, `indicator_state` (industry rows), and `signal_events` — this module joins them |
| `benchmark_rerank.py` | Phase 6-3 composite rank score for ordering BUY signals in email. `rank_score = 0.5·stock_RS_26w + 0.3·industry_RS_26w + 0.2·R²_26w` |
| `benchmark_rerank_client.py` | Re-export of `fetch_rank_scores` for the GUI. Keeps the GUI from importing across into `notifications/` directly — layering hygiene |
| `benchmark_stats_compute.py` | Phase 6-3 regression math. Pure functions, no DB I/O. Per-week arithmetic returns; α, β, RS, R² over rolling 26-week windows |
| `benchmark_stats_runners.py` | Phase 6-3 orchestration. Fetch → compute → write loop for `stock_benchmark_stats` and `industry_benchmark_stats` |

## Pipeline

```
SignalEvent (committed)
   │
   ▼
SubscriberDistributor (scheduler/) — fans out due subscribers
   │
   ▼
NotificationBatch (per subscriber, with rank-re-ordered BUYs)
   │
   ├─ enrichment.enrich_batch  (per-ticker lookups)
   │
   ├─ batcher.render_text  ───┐
   ├─ batcher.render_html  ───┤
   └─ batcher.render_webhook ─┘
                              ▼
                       NotificationTransport (email / webhook / …)
                              │
                              └─ wrapped by CircuitBreakingTransport
                              │
                              └─ delivery attempt → notification_log row
```

## Idempotency

The distributor uses `redis_queue`'s dedup HASH to guarantee
at-most-one enqueue per `(subscriber_id, signal_event_id)`. Transport
failure → False → retry; transport bug → exception → DLQ.

## Brevo specifics

- Key file: `secrets/brevo.yaml` (chmod 600, gitignored).
- Loader: `config/brevo_secrets.py`. Fail-fast at startup if missing,
  world/group-readable, or `api_key` prefix ≠ `xkeysib-`.
- Transport initialises **lazily on first send**, not at boot.
  Absence of an init log line after restart is not a regression.

## Obsolete

- SMTP transport (Outlook.com personal: 5.7.139; Gmail: OAuth-only
  since Sept 2024)
- `TRIP_EMA_SMTP_HOST` env var and any raw-SMTP config fields
