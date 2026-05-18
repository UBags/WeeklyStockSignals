"""Tests for the RedisQueue DLQ + attempts-counter additions
(Phase 12). Uses fakeredis — no real Redis needed."""

from __future__ import annotations

import json

import fakeredis
import pytest

from trip_ema.notifications.redis_queue import RedisQueue


@pytest.fixture
def redis_client():
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def queue(redis_client):
    return RedisQueue(
        redis_client=redis_client,
        queue_name="test:jobs",
        dedup_hash_name="test:dedup",
    )


# ---------- Attempts counter ---------- #


def test_attempts_starts_at_zero(queue):
    assert queue.get_attempts("nonexistent") == 0


def test_increment_attempts_returns_new_value(queue):
    assert queue.increment_attempts("job-1") == 1
    assert queue.increment_attempts("job-1") == 2
    assert queue.increment_attempts("job-1") == 3
    assert queue.get_attempts("job-1") == 3


def test_increment_attempts_is_per_job(queue):
    queue.increment_attempts("job-1")
    queue.increment_attempts("job-1")
    queue.increment_attempts("job-2")
    assert queue.get_attempts("job-1") == 2
    assert queue.get_attempts("job-2") == 1


def test_clear_attempts_resets_to_zero(queue):
    queue.increment_attempts("job-1")
    queue.increment_attempts("job-1")
    queue.clear_attempts("job-1")
    assert queue.get_attempts("job-1") == 0


def test_ack_clears_attempts(queue):
    """A successful delivery (ack) zeroes the failure history for
    that job_id."""
    queue.enqueue("job-1", {"foo": "bar"})
    queue.increment_attempts("job-1")
    queue.increment_attempts("job-1")
    queue.ack("job-1")
    assert queue.get_attempts("job-1") == 0


def test_nack_does_not_clear_attempts(queue):
    """nack frees the dedup slot but preserves the attempts counter
    so subsequent failures accumulate."""
    queue.enqueue("job-1", {"foo": "bar"})
    queue.increment_attempts("job-1")
    queue.nack("job-1")
    assert queue.get_attempts("job-1") == 1


# ---------- DLQ basics ---------- #


def test_dlq_starts_empty(queue):
    assert queue.dlq_depth() == 0
    assert queue.list_dlq() == []


def test_move_to_dlq_pushes_payload(queue):
    queue.enqueue("job-1", {"transport": "email", "subscriber_id": 7})
    job = queue.pop_blocking(timeout_seconds=1)
    queue.move_to_dlq(job, reason="max_attempts (5) exceeded")

    assert queue.dlq_depth() == 1
    entries = queue.list_dlq()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["_job_id"] == "job-1"
    assert entry["_dlq_reason"] == "max_attempts (5) exceeded"
    assert "_dlq_at" in entry


def test_move_to_dlq_clears_dedup_and_attempts(queue):
    """The dedup slot is freed and attempts is cleared when moving
    to DLQ — the job is now terminus, not in retry."""
    queue.enqueue("job-1", {"foo": "bar"})
    queue.increment_attempts("job-1")
    queue.increment_attempts("job-1")
    queue.increment_attempts("job-1")
    job = queue.pop_blocking(timeout_seconds=1)
    queue.move_to_dlq(job, reason="exhausted")

    assert queue.in_flight_count() == 0   # dedup cleared
    assert queue.get_attempts("job-1") == 0  # attempts cleared
    # A fresh enqueue of the same job_id should now work
    assert queue.enqueue("job-1", {"foo": "baz"}) is True


# ---------- DLQ list ---------- #


def test_list_dlq_returns_most_recent_first(queue):
    queue.enqueue("job-a", {"_": 1})
    job_a = queue.pop_blocking(timeout_seconds=1)
    queue.move_to_dlq(job_a, reason="first")

    queue.enqueue("job-b", {"_": 2})
    job_b = queue.pop_blocking(timeout_seconds=1)
    queue.move_to_dlq(job_b, reason="second")

    entries = queue.list_dlq()
    assert len(entries) == 2
    # LPUSH order — most-recent first
    assert entries[0]["_job_id"] == "job-b"
    assert entries[1]["_job_id"] == "job-a"


def test_list_dlq_respects_limit(queue):
    for i in range(5):
        queue.enqueue(f"job-{i}", {"i": i})
        job = queue.pop_blocking(timeout_seconds=1)
        queue.move_to_dlq(job, reason="test")

    assert queue.dlq_depth() == 5
    assert len(queue.list_dlq(limit=2)) == 2
    assert len(queue.list_dlq(limit=5)) == 5
    assert len(queue.list_dlq(limit=10)) == 5  # cap at actual count


def test_list_dlq_skips_malformed_payloads(queue, redis_client):
    """If something stuffs invalid JSON into the DLQ, list_dlq logs
    and skips rather than crashing."""
    redis_client.lpush("test:jobs:dlq", "not-valid-json")
    queue.enqueue("job-good", {"ok": True})
    job = queue.pop_blocking(timeout_seconds=1)
    queue.move_to_dlq(job, reason="test")

    entries = queue.list_dlq()
    # Only the well-formed entry survives
    assert len(entries) == 1
    assert entries[0]["_job_id"] == "job-good"


# ---------- DLQ replay ---------- #


def test_replay_from_dlq_moves_back_to_main_queue(queue):
    queue.enqueue("job-1", {"transport": "email"})
    job = queue.pop_blocking(timeout_seconds=1)
    queue.move_to_dlq(job, reason="test")

    assert queue.queue_depth() == 0
    assert queue.dlq_depth() == 1

    ok = queue.replay_from_dlq("job-1")
    assert ok is True
    assert queue.dlq_depth() == 0
    assert queue.queue_depth() == 1

    replayed = queue.pop_blocking(timeout_seconds=1)
    assert replayed["_job_id"] == "job-1"
    assert replayed["transport"] == "email"
    # DLQ-specific fields removed
    assert "_dlq_reason" not in replayed
    assert "_dlq_at" not in replayed
    # Attempts reset on the payload itself
    assert replayed["_attempts"] == 0


def test_replay_resets_persistent_attempts_counter(queue):
    """After replay, the Redis attempts HASH for that job_id should
    be cleared so the next failure starts from 1, not from where
    the previous cycle left off."""
    queue.enqueue("job-1", {"_": 1})
    queue.increment_attempts("job-1")
    queue.increment_attempts("job-1")
    queue.increment_attempts("job-1")
    job = queue.pop_blocking(timeout_seconds=1)
    queue.move_to_dlq(job, reason="exhausted")
    # move_to_dlq itself clears attempts — sanity
    assert queue.get_attempts("job-1") == 0

    queue.replay_from_dlq("job-1")
    # Still zero post-replay
    assert queue.get_attempts("job-1") == 0


def test_replay_re_establishes_dedup_slot(queue):
    """After replay, the dedup slot is back so a duplicate enqueue
    would be rejected — preserves the original delivery semantics."""
    queue.enqueue("job-1", {"_": 1})
    job = queue.pop_blocking(timeout_seconds=1)
    queue.move_to_dlq(job, reason="test")
    queue.replay_from_dlq("job-1")

    # Now dedup should be set, so a duplicate enqueue is rejected
    assert queue.enqueue("job-1", {"_": 2}) is False


def test_replay_missing_job_returns_false(queue):
    assert queue.replay_from_dlq("does-not-exist") is False


# ---------- DLQ delete ---------- #


def test_delete_from_dlq_removes_entry(queue):
    queue.enqueue("job-1", {"_": 1})
    job = queue.pop_blocking(timeout_seconds=1)
    queue.move_to_dlq(job, reason="test")
    assert queue.dlq_depth() == 1

    ok = queue.delete_from_dlq("job-1")
    assert ok is True
    assert queue.dlq_depth() == 0


def test_delete_missing_job_returns_false(queue):
    assert queue.delete_from_dlq("does-not-exist") is False


def test_delete_only_removes_matching_job(queue):
    for i in range(3):
        queue.enqueue(f"job-{i}", {"i": i})
        job = queue.pop_blocking(timeout_seconds=1)
        queue.move_to_dlq(job, reason="test")

    queue.delete_from_dlq("job-1")
    remaining = {e["_job_id"] for e in queue.list_dlq()}
    assert remaining == {"job-0", "job-2"}
