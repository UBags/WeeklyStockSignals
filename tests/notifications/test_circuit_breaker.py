"""Tests for the CircuitBreakingTransport state machine.

Coverage:
  - CLOSED → OPEN after threshold consecutive failures
  - OPEN → fast-fail (transport not called)
  - OPEN → HALF_OPEN after reset timeout
  - HALF_OPEN probe failure → back to OPEN
  - HALF_OPEN probe success → CLOSED
  - half_open_max_successes > 1 requires that many probes
  - Successful calls in CLOSED state reset failure counter
  - Wrapped transport raising an exception is treated as a failure
    (and breaker logic continues working)
  - Per-transport isolation: two breakers' states don't interfere
  - Metrics emit correctly on state transitions

The breaker uses `time.monotonic()` internally. To exercise the
reset-timeout transition we monkeypatch that function rather than
sleeping in tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from trip_ema.domain import NotificationBatch, SignalEvent, TickerRef, Timeframe, ChangeSignal
from trip_ema.notifications.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakingTransport,
    CircuitState,
)


# -------------------- Doubles -------------------- #


class FakeTransport:
    """A transport whose send() outcome is programmable."""

    def __init__(self, name: str = "fake"):
        self.name_attr = name
        self._return_values: list[bool] = []
        self._raise: Exception | None = None
        self.calls: list[tuple[str, NotificationBatch]] = []

    def queue(self, *return_values: bool) -> None:
        """Queue up the next N return values (one per send call)."""
        self._return_values.extend(return_values)

    def will_raise(self, exc: Exception) -> None:
        """Make the next send() raise. Cleared after one call."""
        self._raise = exc

    def send(self, address: str, batch: NotificationBatch) -> bool:
        self.calls.append((address, batch))
        if self._raise is not None:
            e = self._raise
            self._raise = None
            raise e
        if not self._return_values:
            return True
        return self._return_values.pop(0)


def _make_batch() -> NotificationBatch:
    return NotificationBatch(
        subscriber_id=1,
        events=[SignalEvent(
            ticker=TickerRef("NSE", "NIFTY", "INDEX"),
            timeframe=Timeframe.WEEKLY,
            bar_timestamp=datetime(2026, 5, 4, tzinfo=timezone.utc),
            signal=ChangeSignal.BUY,
            price_at_signal=22000.0,
            computed_at=datetime(2026, 5, 4, tzinfo=timezone.utc),
        )],
        composed_at=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )


# -------------------- Tests -------------------- #


class TestClosedState:
    """CLOSED is the normal state — calls forward; failures accumulate
    until the threshold."""

    def test_successful_calls_stay_closed(self):
        fake = FakeTransport()
        fake.queue(True, True, True)
        cb = CircuitBreakingTransport(
            fake, name="t1",
            config=CircuitBreakerConfig(failure_threshold=3),
        )
        for _ in range(3):
            assert cb.send("addr", _make_batch()) is True
        assert cb.state == CircuitState.CLOSED
        assert len(fake.calls) == 3

    def test_failures_below_threshold_stay_closed(self):
        fake = FakeTransport()
        fake.queue(False, False)
        cb = CircuitBreakingTransport(
            fake, name="t1",
            config=CircuitBreakerConfig(failure_threshold=3),
        )
        cb.send("a", _make_batch())
        cb.send("a", _make_batch())
        assert cb.state == CircuitState.CLOSED

    def test_success_resets_failure_counter(self):
        """A success in CLOSED state resets the counter. With threshold=3,
        a F,F,S,F,F sequence should NOT trip the breaker."""
        fake = FakeTransport()
        fake.queue(False, False, True, False, False)
        cb = CircuitBreakingTransport(
            fake, name="t1",
            config=CircuitBreakerConfig(failure_threshold=3),
        )
        for _ in range(5):
            cb.send("a", _make_batch())
        assert cb.state == CircuitState.CLOSED


class TestClosedToOpenTransition:

    def test_consecutive_failures_at_threshold_trip_to_open(self):
        fake = FakeTransport()
        fake.queue(False, False, False)
        cb = CircuitBreakingTransport(
            fake, name="t1",
            config=CircuitBreakerConfig(failure_threshold=3),
        )
        cb.send("a", _make_batch())
        assert cb.state == CircuitState.CLOSED
        cb.send("a", _make_batch())
        assert cb.state == CircuitState.CLOSED
        cb.send("a", _make_batch())
        assert cb.state == CircuitState.OPEN

    def test_wrapped_exception_counts_as_failure(self):
        """If the wrapped transport raises, the breaker must treat that
        as a failure (not propagate the exception)."""
        fake = FakeTransport()
        cb = CircuitBreakingTransport(
            fake, name="t1",
            config=CircuitBreakerConfig(failure_threshold=2),
        )
        fake.will_raise(RuntimeError("smtp down"))
        result = cb.send("a", _make_batch())
        assert result is False
        # Second failure trips
        fake.will_raise(RuntimeError("still down"))
        cb.send("a", _make_batch())
        assert cb.state == CircuitState.OPEN


class TestOpenState:

    def test_open_state_fast_fails_without_calling_wrapped(self, monkeypatch):
        """When OPEN, send() must NOT call the wrapped transport."""
        # Freeze the clock to a known point BEFORE tripping the breaker
        # so opened_at and the post-trip _now() comparison are both
        # against controlled values, not against time.monotonic()
        # (which depends on container uptime).
        import trip_ema.notifications.circuit_breaker as cb_mod
        monkeypatch.setattr(cb_mod, "_now", lambda: 100.0)

        fake = FakeTransport()
        fake.queue(False, False, False)
        cb = CircuitBreakingTransport(
            fake, name="t1",
            config=CircuitBreakerConfig(
                failure_threshold=3, reset_timeout_seconds=30.0,
            ),
        )
        # Trip the breaker — opened_at gets set to 100.0
        for _ in range(3):
            cb.send("a", _make_batch())
        assert cb.state == CircuitState.OPEN
        call_count_before = len(fake.calls)

        # Still at _now=100.0 (well within reset_timeout). Subsequent
        # calls must return False without invoking the wrapped transport.
        result = cb.send("a", _make_batch())
        assert result is False
        assert len(fake.calls) == call_count_before


class TestOpenToHalfOpenTransition:

    def test_reset_timeout_moves_open_to_half_open_on_next_call(self, monkeypatch):
        fake = FakeTransport()
        fake.queue(False, False)  # Trip the breaker
        cb = CircuitBreakingTransport(
            fake, name="t1",
            config=CircuitBreakerConfig(
                failure_threshold=2, reset_timeout_seconds=10.0,
            ),
        )

        import trip_ema.notifications.circuit_breaker as cb_mod
        # Time 0: send failure, send failure → OPEN
        monkeypatch.setattr(cb_mod, "_now", lambda: 0.0)
        cb.send("a", _make_batch())
        cb.send("a", _make_batch())
        assert cb.state == CircuitState.OPEN

        # Time 5 (< 10s): still OPEN, fast-fail
        monkeypatch.setattr(cb_mod, "_now", lambda: 5.0)
        cb.send("a", _make_batch())
        assert cb.state == CircuitState.OPEN

        # Time 11 (>= 10s): the next call transitions OPEN → HALF_OPEN
        # AND probes (so we need a return value queued for that probe)
        fake.queue(True)
        monkeypatch.setattr(cb_mod, "_now", lambda: 11.0)
        result = cb.send("a", _make_batch())
        # Probe succeeded → state is now CLOSED, result is True
        assert result is True
        assert cb.state == CircuitState.CLOSED


class TestHalfOpenProbe:

    def _trip_to_open(self, cb, fake, monkeypatch, now=0.0):
        import trip_ema.notifications.circuit_breaker as cb_mod
        monkeypatch.setattr(cb_mod, "_now", lambda: now)
        # Queue exactly the threshold-many failures — no extras
        fake.queue(*([False] * cb._config.failure_threshold))
        for _ in range(cb._config.failure_threshold):
            cb.send("a", _make_batch())
        assert cb.state == CircuitState.OPEN

    def test_half_open_probe_failure_returns_to_open(self, monkeypatch):
        fake = FakeTransport()
        cb = CircuitBreakingTransport(
            fake, name="t1",
            config=CircuitBreakerConfig(
                failure_threshold=2, reset_timeout_seconds=10.0,
            ),
        )
        self._trip_to_open(cb, fake, monkeypatch)

        # Move past reset timeout — next call probes
        import trip_ema.notifications.circuit_breaker as cb_mod
        monkeypatch.setattr(cb_mod, "_now", lambda: 20.0)
        fake.queue(False)  # probe fails
        result = cb.send("a", _make_batch())
        assert result is False
        assert cb.state == CircuitState.OPEN

    def test_half_open_probe_success_returns_to_closed(self, monkeypatch):
        fake = FakeTransport()
        cb = CircuitBreakingTransport(
            fake, name="t1",
            config=CircuitBreakerConfig(
                failure_threshold=2, reset_timeout_seconds=10.0,
                half_open_max_successes=1,
            ),
        )
        self._trip_to_open(cb, fake, monkeypatch)
        import trip_ema.notifications.circuit_breaker as cb_mod
        monkeypatch.setattr(cb_mod, "_now", lambda: 20.0)
        fake.queue(True)  # probe succeeds
        result = cb.send("a", _make_batch())
        assert result is True
        assert cb.state == CircuitState.CLOSED

    def test_half_open_requires_max_successes_to_close(self, monkeypatch):
        """With half_open_max_successes=3, three consecutive probes
        must succeed before returning to CLOSED."""
        fake = FakeTransport()
        cb = CircuitBreakingTransport(
            fake, name="t1",
            config=CircuitBreakerConfig(
                failure_threshold=2, reset_timeout_seconds=10.0,
                half_open_max_successes=3,
            ),
        )
        self._trip_to_open(cb, fake, monkeypatch)

        import trip_ema.notifications.circuit_breaker as cb_mod
        # Probe 1: success — still HALF_OPEN
        monkeypatch.setattr(cb_mod, "_now", lambda: 20.0)
        fake.queue(True)
        assert cb.send("a", _make_batch()) is True
        assert cb.state == CircuitState.HALF_OPEN

        # Probe 2: success — still HALF_OPEN
        fake.queue(True)
        assert cb.send("a", _make_batch()) is True
        assert cb.state == CircuitState.HALF_OPEN

        # Probe 3: success — finally CLOSED
        fake.queue(True)
        assert cb.send("a", _make_batch()) is True
        assert cb.state == CircuitState.CLOSED


class TestPerTransportIsolation:

    def test_two_breakers_dont_share_state(self):
        """Each CircuitBreakingTransport instance has its own state.
        Tripping one must not affect the other."""
        fake1 = FakeTransport(name="email")
        fake2 = FakeTransport(name="webhook")
        cb1 = CircuitBreakingTransport(
            fake1, name="email",
            config=CircuitBreakerConfig(failure_threshold=2),
        )
        cb2 = CircuitBreakingTransport(
            fake2, name="webhook",
            config=CircuitBreakerConfig(failure_threshold=2),
        )

        fake1.queue(False, False)
        cb1.send("a", _make_batch())
        cb1.send("a", _make_batch())
        assert cb1.state == CircuitState.OPEN

        # cb2 is unaffected; calls still forward
        fake2.queue(True)
        assert cb2.send("a", _make_batch()) is True
        assert cb2.state == CircuitState.CLOSED


class TestMetricsEmissions:

    def test_state_gauge_reflects_current_state(self):
        from trip_ema.notifications.circuit_breaker import _ensure_metrics
        state_gauge, _ = _ensure_metrics()

        fake = FakeTransport()
        cb = CircuitBreakingTransport(
            fake, name="metrics_test_t",
            config=CircuitBreakerConfig(failure_threshold=2),
        )
        # On construction, the gauge for CLOSED is 1; OPEN and HALF_OPEN are 0
        closed_value = state_gauge.labels(
            transport="metrics_test_t", state="closed",
        )._value.get()
        assert closed_value == 1.0

        # Trip
        fake.queue(False, False)
        cb.send("a", _make_batch())
        cb.send("a", _make_batch())

        # Now OPEN gauge is 1, CLOSED is 0
        closed_now = state_gauge.labels(
            transport="metrics_test_t", state="closed",
        )._value.get()
        open_now = state_gauge.labels(
            transport="metrics_test_t", state="open",
        )._value.get()
        assert closed_now == 0.0
        assert open_now == 1.0

    def test_transition_counter_increments(self):
        from trip_ema.notifications.circuit_breaker import _ensure_metrics
        _, transitions = _ensure_metrics()

        fake = FakeTransport()
        cb = CircuitBreakingTransport(
            fake, name="trans_test_t",
            config=CircuitBreakerConfig(failure_threshold=2),
        )
        before = transitions.labels(
            transport="trans_test_t", from_state="closed", to_state="open",
        )._value.get()
        fake.queue(False, False)
        cb.send("a", _make_batch())
        cb.send("a", _make_batch())
        after = transitions.labels(
            transport="trans_test_t", from_state="closed", to_state="open",
        )._value.get()
        assert after == before + 1.0


class TestWorkerInteractionViaBreaker:
    """Sanity that NotifierWorker treats a breaker-OPEN return-False
    the same as a normal transport failure (nack + retry)."""

    def test_breaker_open_returns_false_to_worker_path(self):
        """We don't run the full worker here — just verify the
        contract: cb.send() returns a bool, never raises, and an OPEN
        breaker returns False."""
        fake = FakeTransport()
        fake.queue(False, False)
        cb = CircuitBreakingTransport(
            fake, name="worker_test",
            config=CircuitBreakerConfig(failure_threshold=2),
        )
        cb.send("a", _make_batch())
        cb.send("a", _make_batch())
        # Now OPEN
        result = cb.send("a", _make_batch())
        assert isinstance(result, bool)
        assert result is False
