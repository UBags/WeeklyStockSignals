# Notifier dead-letter queue (DLQ)

Phase 12 added a dead-letter queue to the notifier worker. When a
single notification job fails to deliver `max_attempts` times in a
row, it moves to the DLQ instead of cycling forever between the
distributor and the worker.

## When does this matter?

The pre-Phase-12 failure mode: a transport's circuit breaker opens
(e.g., SMTP server down). The worker receives jobs, fails to send,
nacks them. The distributor's next cycle sees the same pending
subscriptions and re-enqueues. The worker fails them again. The
loop runs until the circuit closes — which might be hours or days
for an actual outage.

With the DLQ:

- The worker increments a **persistent** attempt counter in Redis
  on each failure (lives in a HASH, not on the payload, so it
  survives the distributor recreating the payload)
- When attempts ≥ `notifier.max_attempts` (default 5), the worker
  moves the job to the DLQ
- The dedup slot is freed and the attempts counter is cleared
- An operator inspects the DLQ via the admin API, fixes the
  underlying issue, then replays the job(s)

## Admin endpoints

All require the admin bearer token (`TRIP_EMA_ADMIN_TOKEN` env var).

### List

```http
GET /v1/admin/notifications/dlq?limit=100
Authorization: Bearer <token>
```

Returns most-recent first:

```json
{
  "entries": [
    {
      "job_id": "sub:42:event:1001",
      "transport": "email",
      "subscriber_id": 42,
      "event_id": 1001,
      "attempts": 5,
      "failed_at": "2026-05-18T07:23:11Z",
      "reason": "max_attempts (5) exceeded",
      "raw_payload": { ... }
    }
  ],
  "total": 1,
  "snapshot_taken_at": "2026-05-18T07:30:00Z"
}
```

`total` may exceed `len(entries)` — the response is bounded by
`limit`. If `total > len(entries)`, raise `limit` to see more.

### Replay

```http
POST /v1/admin/notifications/dlq/{job_id}/replay
Authorization: Bearer <token>
```

Moves the job back to the main queue. Resets the attempts counter.
The dedup slot is re-established so a duplicate enqueue from the
distributor is rejected.

Use after the underlying transport problem has been fixed. If the
problem persists, the job will hit `max_attempts` again and return
to the DLQ.

### Delete

```http
DELETE /v1/admin/notifications/dlq/{job_id}
Authorization: Bearer <token>
```

Permanently removes the job. Use for jobs that should never be
retried (e.g., a misconfigured subscriber that no longer exists).

## Tuning

`notifier.max_attempts` defaults to 5. Set lower for transports
where you want to give up fast (e.g., webhooks that should respond
instantly); set higher for transports that experience transient
outages (e.g., SMTP servers that occasionally drop connections).

## Metrics

The Prometheus counter
`trip_ema_notifier_jobs_processed_total{outcome="dlq"}` increments
on each DLQ move. Alert on rate-of-increase to catch transport
problems early.
