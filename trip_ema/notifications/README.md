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
| `batcher.py` | Renders one event line (text or JSON) for the message body. Single-event today; reserved for multi-event batching |
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

## Tests

- `tests/notifications/test_batcher.py`
- `tests/notifications/test_circuit_breaker.py`
- `tests/notifications/test_redis_queue.py`, `test_redis_queue_dlq.py`
- `tests/notifications/test_transports.py`
- `tests/notifier/test_notifier_worker.py` (DB-required)
- `tests/notifier_pure/test_worker_dlq.py`,
  `tests/notifier_pure/test_worker_staleness.py` (no DB)
