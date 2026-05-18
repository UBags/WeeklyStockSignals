"""Tiny Redis-backed work queue with atomic dedup.

Two data structures live in Redis:

1. A LIST at `queue_name` — the actual FIFO of pending jobs. Producers
   LPUSH; consumers BRPOP. Payloads are JSON strings.

2. A HASH at `dedup_hash_name` — keyed by job_id, value is the enqueue
   timestamp (epoch seconds). Used to suppress double-enqueues for the
   same logical job while one is in flight.

The combination is operated atomically via Lua scripts:
  - `enqueue(job_id, payload)` checks the dedup hash; if absent, adds the
    field AND pushes the payload — both in one script. No race window.
  - `ack(job_id)` removes the dedup entry. Called after the worker
    successfully delivers the job. Subsequent enqueue attempts for the
    same job_id will succeed again — which is the right behavior, since
    "re-enqueue after ack" matches the distributor's logic: if the same
    (sub, event) is still pending after the worker finished a send and
    decremented the remaining-sends count, the next distributor pass
    re-enqueues for the next send.

Payloads are JSON — the worker doesn't need to share Python types with
the producer. A separate consumer in a different language/runtime could
plug in without changes.
"""

from __future__ import annotations

import json
import logging
import time as time_module
from typing import Any

import redis

logger = logging.getLogger(__name__)


# Lua script for atomic enqueue. Returns 1 if pushed, 0 if dedup-blocked.
# KEYS[1] = queue list name
# KEYS[2] = dedup hash name
# ARGV[1] = job_id
# ARGV[2] = payload (JSON string)
# ARGV[3] = current timestamp (epoch seconds, as string)
_ENQUEUE_SCRIPT = """
if redis.call("HEXISTS", KEYS[2], ARGV[1]) == 1 then
    return 0
end
redis.call("HSET", KEYS[2], ARGV[1], ARGV[3])
redis.call("LPUSH", KEYS[1], ARGV[2])
return 1
"""


class RedisQueue:
    """A small FIFO with at-most-one-in-flight dedup per job_id."""

    def __init__(
        self,
        redis_client: redis.Redis,
        queue_name: str,
        dedup_hash_name: str,
    ):
        self._r = redis_client
        self._queue = queue_name
        self._dedup = dedup_hash_name
        # Register the Lua script with Redis — script registration is
        # idempotent (SCRIPT LOAD is keyed by script content's SHA).
        self._enqueue_script = self._r.register_script(_ENQUEUE_SCRIPT)

    def enqueue(self, job_id: str, payload: dict[str, Any]) -> bool:
        """Enqueue a job if `job_id` isn't already in flight.

        Returns True if the job was pushed, False if dedup-blocked.
        """
        # The payload must include the job_id so the worker can ack.
        enriched = dict(payload)
        enriched["_job_id"] = job_id
        result = self._enqueue_script(
            keys=[self._queue, self._dedup],
            args=[job_id, json.dumps(enriched), str(int(time_module.time()))],
        )
        return bool(result)

    def pop_blocking(self, timeout_seconds: int = 5) -> dict[str, Any] | None:
        """Pop one job, blocking up to `timeout_seconds`. Returns the
        payload dict, or None on timeout.

        The dedup entry is NOT cleared here — the consumer should call
        `ack()` after successful delivery to free the slot. If the
        consumer crashes mid-send, the dedup entry persists; the
        distributor's next pass will be blocked from re-enqueueing the
        same job_id. That's the conservative choice (no duplicate
        notifications); the cost is that a crashed worker leaves jobs
        stuck. Operationally, a periodic sweep can clear old dedup
        entries — see `expire_old_dedup_entries()`.
        """
        result = self._r.brpop([self._queue], timeout=timeout_seconds)
        if result is None:
            return None
        _, raw = result    # (queue_name, payload_bytes)
        return json.loads(raw)

    def ack(self, job_id: str) -> bool:
        """Remove the dedup entry for `job_id`. Returns True if an entry
        was actually removed, False if no entry existed (already ack'd or
        never enqueued)."""
        return bool(self._r.hdel(self._dedup, job_id))

    def nack(self, job_id: str) -> bool:
        """Mark a job as failed and free its dedup slot immediately.

        Semantically identical to ack() — both just HDEL the dedup entry —
        but exists as a separate method so callers can distinguish
        success-paths from failure-paths in their code, and so future
        changes (e.g., recording failure metrics, requeueing with backoff)
        can be added here without touching ack().

        Without this call, a failed send would leave the dedup slot held
        until `expire_old_dedup_entries()` clears it (default 1 hour),
        delaying retry by up to that long even though the distributor's
        next cycle would otherwise re-enqueue immediately. Calling nack()
        on transport failure restores the "retry on next cycle" contract.
        """
        return bool(self._r.hdel(self._dedup, job_id))

    def in_flight_count(self) -> int:
        """Number of jobs currently dedup-tracked (queued + being processed)."""
        return int(self._r.hlen(self._dedup))

    def queue_depth(self) -> int:
        """Number of jobs sitting in the queue (not yet popped)."""
        return int(self._r.llen(self._queue))

    def expire_old_dedup_entries(self, max_age_seconds: int) -> int:
        """Remove dedup entries whose enqueue timestamp is older than
        `max_age_seconds`. Returns the count removed.

        Use this in a periodic sweep to recover from crashed workers
        that never acked. Set max_age to comfortably exceed your worst
        expected delivery latency.
        """
        cutoff = int(time_module.time()) - max_age_seconds
        removed = 0
        # HGETALL is O(N); fine for the expected scale (hundreds of
        # in-flight at most). If it ever grows large, switch to HSCAN.
        all_entries = self._r.hgetall(self._dedup)
        for raw_field, raw_ts in all_entries.items():
            try:
                ts = int(raw_ts)
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                self._r.hdel(self._dedup, raw_field)
                removed += 1
        if removed:
            logger.warning(
                "Expired %d stale dedup entries (older than %ds)",
                removed, max_age_seconds,
            )
        return removed

    def ping(self) -> bool:
        """Health check — just whether Redis responds."""
        try:
            return bool(self._r.ping())
        except redis.RedisError:
            return False
