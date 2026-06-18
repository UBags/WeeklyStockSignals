# `trip_ema/notifications/` — Transports, batcher, breakers, Redis queue

Everything between "the engine decided a regime changed" and "an email
arrives in the subscriber's inbox". Three transports (email, webhook,
Slack), per-transport circuit breakers, a Redis queue producer +
consumer abstraction, and a small batcher that renders the message body.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `base.py` | Abstract `NotificationTransport` + `NullTransport` (used by tests) |
| `email_transport.py` | Brevo HTTP API delivery (POST to `/v3/smtp/email`). Replaced the SMTP implementation after Microsoft disabled `SmtpClientAuthentication` for new Outlook.com personal mailboxes and Gmail moved to OAuth-only |
| `webhook_transport.py` | HTTP POST delivery (Slack-compatible JSON body) |
| `circuit_breaker.py` | Per-transport failure-window breaker — opens after `failure_threshold` consecutive failures within a window; closes after `reset_timeout_seconds` of quiet; half-open lets `half_open_max_successes` succeed before fully closing |
| `enrichment.py` | Per-batch enrichment lookups for HTML email rendering — resolves each ticker's market cap, sector/industry, sector zone (display_regime), sector direction (day-over-day industry-index delta), and last 5 signal events. `enrich_batch(engine, batch)` runs four SQL queries and returns a `BatchEnrichment` container. Degrades gracefully: missing data → None/0 → renderer prints `—`. Errors (DB down) are caught and return `EMPTY_ENRICHMENT` so email delivery never blocks on enrichment |
| `batcher.py` | Renders `NotificationBatch` into three formats: **text** (one event per line), **HTML** (grouped by signal type, color-coded tables with per-ticker enrichment columns: Mkt Cap, Sector, Sector Zone, Sector Direction, Last 5 signals), and **JSON** (structured array for webhooks). HTML sections emitted in fixed order (BUY → SELL → Exit Buy → Exit Sell); within a section rows sorted alphabetically by symbol. All styles inline (Gmail strips `<style>` blocks). Indian-numbering helper formats market cap as `₹19,84,302 Cr` |
| `redis_queue.py` | `RedisQueue` producer (used by `SubscriberDistributor`) + consumer (used by `NotifierWorker`). Also owns the DLQ data structure and the Redis HASH attempt counter |

## Email transport (Brevo HTTP API)

`email_transport.py` posts to Brevo's transactional `/v3/smtp/email`
endpoint with an `api-key` header. The key itself is read at startup
from a YAML file outside the main config (see
`../config/brevo_secrets.py` and `transports.brevo.secrets_file` in
`config.yaml`); the loader validates file perms and key prefix, then
crashes the process on any defect — there is **no env-var fallback**
that could silently substitute `NullTransport` for misconfigured
email. The old SMTP implementation's `TRIP_EMA_SMTP_HOST`-unset path
lost weeks of notifications by silently substituting `NullTransport`
at send time; the new contract makes that bug class structurally
impossible.

Sender verification is enforced by Brevo itself: the `From:` address
listed in `transports.brevo.sender_email` must be confirmed in
Brevo's Senders settings, or every send returns
`400 invalid_parameter`. Setup is a one-time email-click flow.

## Notifier worker

The consumer side lives in `../notifier_worker.py` (top-level module,
not in this directory). Architecture decision: the worker is a
separate Python process from the engine — a wedged transport (Brevo
API, webhook target) can't slow down compute.

## Circuit breaker pattern

```
closed (normal)
   N consecutive failures within window
      => open
            after reset_timeout_seconds
                => half_open
                    M consecutive successes
                       => closed
                    any failure
                       => open
```

Defaults: `failure_threshold=5`, `reset_timeout_seconds=30`,
`half_open_max_successes=1`. Configurable in `config.yaml` under
`transports.circuit_breaker:`. When open, jobs go to the DLQ
instead of being retried — protects upstream from amplifying
failures.

## At-least-once + dedup window

Phase 9 invariant: within the dedup window (~1h), a duplicate is
possible but silently de-deduped by `notification_log` hash. Missing
a signal is worse than seeing it twice. The architecture doc §5
documents this as intentional.

## DLQ (Phase 12)

When a notification job fails to deliver `max_attempts` times
(default 5), the worker moves it to a Redis-backed DLQ instead of
cycling it back into the queue. The attempt counter lives in a
persistent Redis HASH keyed by `job_id` — survives the distributor
recreating the payload on each cycle.

Admin endpoints (in `transports/http/routes/admin_notifications.py`):
- `GET /v1/admin/notifications/dlq` — list entries
- `POST /v1/admin/notifications/dlq/{job_id}/replay` — requeue
- `DELETE /v1/admin/notifications/dlq/{job_id}` — permanent delete

## Enrichment → HTML rendering pipeline

When the `SubscriberDistributor` fires in legacy (in-process) mode,
`app.py` wires an `enrichment_provider` closure into the
`EmailTransport`. On each `send()`, the transport calls:

```
enrichment_provider(batch)
  → enrich_batch(db_engine, batch)
      → _fetch_ticker_basics(conn, refs)    # ticker_id + sector + industry + latest mcap
      → _fetch_sector_zones(conn, industries)  # latest display_regime per industry
      → _fetch_sector_directions(conn, industries)  # sign of day-over-day index-value change
      → _fetch_recent_signals(conn, ticker_ids)  # last 5 signal_events per ticker
  → BatchEnrichment (keyed by (exchange, symbol, instrument))

render_html(batch, enrichment)
  → grouped HTML tables per signal type
```

If enrichment fails (DB down, schema mismatch), `EmailTransport`
falls back to text-only for that send — enrichment is decoration,
not a delivery requirement.

### Known bug (fixed): SQLAlchemy bind-name parsing

`_fetch_ticker_basics` uses Postgres `unnest(:exchanges::text[],
:symbols::text[], :instruments::text[])`. SQLAlchemy's `text()`
parser detects bind names with a `:(\w+)(?!:)` lookahead —
`:exchanges::text[]` caused it to bind `:exchange` (singular)
instead of `:exchanges` (plural). The params dict supplied plural
keys, so `execute()` raised, the broad `except` in `enrich_batch`
swallowed it, and `EMPTY_ENRICHMENT` was returned — every email
row showed `—` / NEUTRAL. Fix: wrap each cast as
`(:exchanges)::text[]`. Regression test:
`tests/notifications/test_enrichment.py`.

## Tests

- `tests/notifications/test_batcher.py`
- `tests/notifications/test_html_render.py`
- `tests/notifications/test_enrichment.py` — regression test for
  the SQLAlchemy bind-name parsing bug in `_fetch_ticker_basics`
- `tests/notifications/test_circuit_breaker.py`
- `tests/notifications/test_redis_queue.py`, `test_redis_queue_dlq.py`
- `tests/notifications/test_transports.py`
- `tests/notifier/test_notifier_worker.py` (DB-required)
- `tests/notifier_pure/test_worker_dlq.py`,
  `tests/notifier_pure/test_worker_staleness.py` (no DB)
