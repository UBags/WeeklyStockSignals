"""Tests for the RedisQueue wrapper. Uses fakeredis — no real Redis needed."""

from __future__ import annotations

import time

import fakeredis
import pytest

from trip_ema.notifications.redis_queue import RedisQueue


@pytest.fixture
def redis_client():
    """A fresh fakeredis instance per test. fakeredis.FakeStrictRedis
    supports Lua scripts via fakeredis>=2.0, which is what we need for
    the atomic enqueue."""
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def queue(redis_client):
    return RedisQueue(
        redis_client=redis_client,
        queue_name="test:jobs",
        dedup_hash_name="test:dedup",
    )


# ---------- Enqueue ---------- #


def test_enqueue_returns_true_for_new_job(queue):
    assert queue.enqueue("job-1", {"foo": "bar"}) is True
    assert queue.queue_depth() == 1
    assert queue.in_flight_count() == 1


def test_enqueue_returns_false_for_duplicate(queue):
    assert queue.enqueue("job-1", {"foo": "bar"}) is True
    # Same job_id, even different payload — should be blocked
    assert queue.enqueue("job-1", {"foo": "baz"}) is False
    assert queue.queue_depth() == 1


def test_different_job_ids_both_enqueue(queue):
    assert queue.enqueue("job-1", {}) is True
    assert queue.enqueue("job-2", {}) is True
    assert queue.queue_depth() == 2


# ---------- Pop ---------- #


def test_pop_returns_payload_including_job_id(queue):
    queue.enqueue("job-1", {"foo": "bar", "n": 42})
    popped = queue.pop_blocking(timeout_seconds=1)
    assert popped is not None
    assert popped["foo"] == "bar"
    assert popped["n"] == 42
    assert popped["_job_id"] == "job-1"


def test_pop_returns_none_on_empty_queue_after_timeout(queue):
    start = time.time()
    result = queue.pop_blocking(timeout_seconds=1)
    elapsed = time.time() - start
    assert result is None
    assert elapsed >= 0.5    # allow some slack


def test_pop_is_fifo(queue):
    queue.enqueue("a", {"i": 1})
    queue.enqueue("b", {"i": 2})
    queue.enqueue("c", {"i": 3})
    assert queue.pop_blocking(1)["_job_id"] == "a"
    assert queue.pop_blocking(1)["_job_id"] == "b"
    assert queue.pop_blocking(1)["_job_id"] == "c"


def test_pop_does_not_clear_dedup(queue):
    """Critical: pop only removes from the LIST. The dedup hash entry
    stays until ack() — preventing the same job from being enqueued
    twice while in flight."""
    queue.enqueue("job-1", {})
    queue.pop_blocking(1)
    assert queue.queue_depth() == 0
    assert queue.in_flight_count() == 1   # still tracked
    # Trying to re-enqueue while in flight should still be blocked
    assert queue.enqueue("job-1", {}) is False


# ---------- Ack ---------- #


def test_ack_clears_dedup(queue):
    queue.enqueue("job-1", {})
    queue.pop_blocking(1)
    assert queue.ack("job-1") is True
    assert queue.in_flight_count() == 0


def test_ack_returns_false_when_no_entry(queue):
    assert queue.ack("never-enqueued") is False


def test_after_ack_same_job_id_can_be_reenqueued(queue):
    """The whole point of ack-then-re-enqueue: when the distributor's
    next pass still finds the same (sub, event) needs another send,
    it should be able to re-enqueue."""
    queue.enqueue("job-1", {"send_num": 1})
    queue.pop_blocking(1)
    queue.ack("job-1")
    assert queue.enqueue("job-1", {"send_num": 2}) is True


def test_nack_clears_dedup_immediately(queue):
    """A2 fix: nack() is used on transport failure to free the dedup
    slot so the next distributor cycle can re-enqueue immediately,
    rather than waiting up to dedup_max_age_seconds."""
    queue.enqueue("job-1", {})
    queue.pop_blocking(1)
    assert queue.nack("job-1") is True
    assert queue.in_flight_count() == 0


def test_after_nack_same_job_id_can_be_reenqueued(queue):
    queue.enqueue("job-1", {})
    queue.pop_blocking(1)
    queue.nack("job-1")
    assert queue.enqueue("job-1", {}) is True


# ---------- Dedup TTL sweep ---------- #


def test_expire_old_dedup_entries(queue, redis_client):
    """Stuck entries (worker crashed before ack) should be cleanable
    by an external sweep."""
    queue.enqueue("stuck-1", {})
    queue.enqueue("stuck-2", {})

    # Backdate both dedup entries to 100 seconds ago
    past = int(time.time()) - 100
    for jid in ("stuck-1", "stuck-2"):
        redis_client.hset("test:dedup", jid, str(past))

    # Sweep with max_age=50 — both should expire
    removed = queue.expire_old_dedup_entries(max_age_seconds=50)
    assert removed == 2
    assert queue.in_flight_count() == 0


def test_expire_keeps_fresh_entries(queue):
    queue.enqueue("fresh", {})
    removed = queue.expire_old_dedup_entries(max_age_seconds=3600)
    assert removed == 0
    assert queue.in_flight_count() == 1


# ---------- Misc ---------- #


def test_ping_succeeds(queue):
    assert queue.ping() is True


def test_queue_depth_zero_on_empty(queue):
    assert queue.queue_depth() == 0
