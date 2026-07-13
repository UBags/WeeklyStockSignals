"""Circuit breaker for notification transports.

Wraps any `NotificationTransport` with a state machine that fast-fails
calls when the underlying transport is sufficiently broken. Three
states:

    CLOSED       — normal operation; calls pass through.
    OPEN         — transport is unhealthy; calls return False
                   immediately without invoking the underlying transport.
    HALF_OPEN    — after a cool-down, allow ONE probe call to test
                   recovery. Result determines next state.

The goal is two-fold:
  1. Protect the underlying transport from request floods while it's
     failing (every retry is a free chance to make things worse).
  2. Provide loud, observable backpressure: when the breaker opens, a
     dashboard / log dive shows immediately *which transport* is unhealthy.

Behavior contract:
  - send() always returns a bool. Never raises (errors from the
    underlying transport are caught and translated to False).
  - From the worker's perspective, an "OPEN return False" is
    indistinguishable from a "real failure return False". The worker
    nacks the job and the distributor's next cycle re-tries. Net
    effect: jobs queue up briefly while a transport is broken, then
    drain when it recovers.
  - Half-open probe is the *single next call*, not a parallel one.
    Concurrent calls during HALF_OPEN race; whichever lands first is
    the probe. This is fine because the breaker is per-transport-
    instance, and the worker uses ThreadPoolExecutor with bounded
    concurrency.

Thread safety:
  A simple `threading.Lock` guards state transitions. We never hold
  the lock across the wrapped `send()` call (that would serialize all
  delivery — the opposite of what we want). The lock only protects
  state reads/writes.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import StrEnum

from ..domain import NotificationBatch
from ..domain.clock import MonotonicClock, monotonic_clock
from ..observability import metrics as M

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Tunables. The defaults are intentionally conservative for a
    trading-alert system: we'd rather over-protect a downstream than
    spam it with retries during an outage."""

    failure_threshold: int = 5
    """How many consecutive failures before tripping to OPEN."""

    reset_timeout_seconds: float = 30.0
    """How long to stay OPEN before transitioning to HALF_OPEN."""

    half_open_max_successes: int = 1
    """How many consecutive HALF_OPEN successes needed to return to
    CLOSED. 1 is the standard CB pattern; >1 is more cautious."""


# Metrics for transport state — registered lazily on first use so that
# importing this module doesn't bloat the metrics registry with
# transports the system isn't using.
_circuit_state_gauge = None
_circuit_transition_counter = None


def _ensure_metrics():
    global _circuit_state_gauge, _circuit_transition_counter
    if _circuit_state_gauge is None:
        try:
            from prometheus_client import Counter, Gauge
            _circuit_state_gauge = Gauge(
                "trip_ema_transport_circuit_state",
                "Current circuit-breaker state per transport "
                "(1 = active state, 0 otherwise; one row per state).",
                labelnames=("transport", "state"),
                # TODO: queued — refactor _NoOpRegistry to inherit
                # CollectorRegistry. Until then, no-op shim is
                # unreachable on this branch (prom_client imported).
                registry=M.REGISTRY,  # pyright: ignore[reportArgumentType]
            )
            _circuit_transition_counter = Counter(
                "trip_ema_transport_circuit_transitions_total",
                "Circuit-breaker state transitions per transport.",
                labelnames=("transport", "from_state", "to_state"),
                # TODO: queued — refactor _NoOpRegistry to inherit
                # CollectorRegistry. Until then, no-op shim is
                # unreachable on this branch (prom_client imported).
                registry=M.REGISTRY,  # pyright: ignore[reportArgumentType]
            )
        except ImportError:
            # prometheus_client not installed — use the no-op shims
            # that metrics.py defines for exactly this case.
            from ..observability.metrics import Counter, Gauge
            _circuit_state_gauge = Gauge(
                "trip_ema_transport_circuit_state",
                "no-op",
                labelnames=("transport", "state"),
            )
            _circuit_transition_counter = Counter(
                "trip_ema_transport_circuit_transitions_total",
                "no-op",
                labelnames=("transport", "from_state", "to_state"),
            )
    return _circuit_state_gauge, _circuit_transition_counter


class CircuitBreakingTransport:
    """Wraps a NotificationTransport with circuit-breaker semantics.

    The wrapped transport is held by reference; we forward all calls
    when the breaker is CLOSED (or during a HALF_OPEN probe) and
    return False without forwarding when OPEN.

    Construct one per underlying transport instance, not per send call.
    The instance accumulates the failure/success counts.
    """

    def __init__(
        self,
        wrapped: NotificationTransportProtocol,
        name: str,
        config: CircuitBreakerConfig | None = None,
        clock: MonotonicClock = monotonic_clock,
    ):
        """`clock` is a MonotonicClock — elapsed-time source for the
        reset-timeout comparison. Defaults to `time.monotonic`. Injected so
        tests drive the OPEN -> HALF_OPEN transition by hand instead of
        sleeping (or patching a module global, which is what this replaced).
        Not part of CircuitBreakerConfig: config is frozen, serialisable
        tunables; the clock is a collaborator.
        """
        self._wrapped = wrapped
        self._name = name
        self._config = config or CircuitBreakerConfig()
        self._clock = clock
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_half_open_successes = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()
        # Initialize metric gauges for this transport
        _ensure_metrics()
        self._publish_state_gauge()

    # ---------- public ---------- #

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    @property
    def name(self) -> str:
        return self._name

    def snapshot(self) -> dict:
        """Return a point-in-time view of the breaker state, suitable
        for an admin/inspection endpoint. Read under the lock so the
        snapshot is internally consistent (state and consecutive_failures
        won't be observed mid-transition).

        Fields:
            name:             transport identifier (e.g. "email", "webhook")
            state:            "closed" | "open" | "half_open"
            consecutive_failures: int
            opened_at_monotonic: float | None — time.monotonic() at last
                              CLOSED→OPEN transition. Useful for computing
                              "how long has this been open?" client-side.
                              Wall-clock time isn't recorded here (the
                              breaker uses monotonic for correctness across
                              NTP adjustments); the caller can subtract
                              from time.monotonic() to get an elapsed
                              duration in seconds.
            config: {failure_threshold, reset_timeout_seconds,
                     half_open_max_successes}
        """
        with self._lock:
            return {
                "name": self._name,
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "opened_at_monotonic": self._opened_at,
                "config": {
                    "failure_threshold": self._config.failure_threshold,
                    "reset_timeout_seconds": self._config.reset_timeout_seconds,
                    "half_open_max_successes": self._config.half_open_max_successes,
                },
            }

    def send(self, address: str, batch: NotificationBatch) -> bool:
        """Forward to the wrapped transport, applying breaker logic."""
        # State decision happens under the lock; the actual call does NOT.
        with self._lock:
            decision = self._decide_action_locked()
        if decision == "fast_fail":
            logger.warning(
                "Circuit OPEN; skipping send",
                extra={"transport": self._name, "address": address},
            )
            return False

        # decision is "send" (CLOSED) or "probe" (HALF_OPEN)
        try:
            ok = bool(self._wrapped.send(address, batch))
        except Exception:
            # Defensive — the protocol says transports don't raise, but
            # a sloppy implementation might. Catch here so the breaker
            # records it as a failure and the worker doesn't see the
            # exception.
            logger.exception(
                "Wrapped transport raised; counting as failure",
                extra={"transport": self._name},
            )
            ok = False

        with self._lock:
            self._record_result_locked(ok)
        return ok

    # ---------- internal ---------- #

    def _decide_action_locked(self) -> str:
        """Return 'send' (CLOSED), 'probe' (HALF_OPEN), or 'fast_fail' (OPEN).

        May transition OPEN → HALF_OPEN if the reset timeout has elapsed.
        Called under lock.
        """
        if self._state == CircuitState.CLOSED:
            return "send"
        if self._state == CircuitState.OPEN:
            # Check if it's time to try a probe
            if self._opened_at is not None and (
                self._clock() - self._opened_at
                >= self._config.reset_timeout_seconds
            ):
                self._transition_locked(CircuitState.HALF_OPEN)
                self._consecutive_half_open_successes = 0
                return "probe"
            return "fast_fail"
        # HALF_OPEN — allow the probe through
        return "probe"

    def _record_result_locked(self, ok: bool) -> None:
        """Update counters and state given the result of a forwarded
        call. Called under lock."""
        if ok:
            if self._state == CircuitState.CLOSED:
                # Reset failure counter; nothing else to do
                self._consecutive_failures = 0
            elif self._state == CircuitState.HALF_OPEN:
                self._consecutive_half_open_successes += 1
                if self._consecutive_half_open_successes >= (
                    self._config.half_open_max_successes
                ):
                    self._transition_locked(CircuitState.CLOSED)
                    self._consecutive_failures = 0
                    self._opened_at = None
            # If we somehow get here while OPEN, ignore — the
            # decision step shouldn't have let the call through.
        else:
            # Failure path
            self._consecutive_failures += 1
            if self._state == CircuitState.HALF_OPEN:
                # Probe failed — back to OPEN with fresh clock
                self._transition_locked(CircuitState.OPEN)
                self._opened_at = self._clock()
                self._consecutive_half_open_successes = 0
            elif self._state == CircuitState.CLOSED:
                if self._consecutive_failures >= self._config.failure_threshold:
                    self._transition_locked(CircuitState.OPEN)
                    self._opened_at = self._clock()

    def _transition_locked(self, new_state: CircuitState) -> None:
        """Record a state transition + emit a log line and bump the
        Prometheus counter. Called under lock."""
        old = self._state
        if old == new_state:
            return
        self._state = new_state
        logger.warning(
            "Transport circuit transition",
            extra={
                "transport": self._name,
                "from_state": old.value,
                "to_state": new_state.value,
            },
        )
        _, transitions = _ensure_metrics()
        assert transitions is not None, (
            "_ensure_metrics returned None transitions counter; "
            "the no-op shim path should set both elements"
        )
        transitions.labels(
            transport=self._name, from_state=old.value, to_state=new_state.value,
        ).inc()
        self._publish_state_gauge()

    def _publish_state_gauge(self) -> None:
        """Set the per-state gauge so dashboards can show a 0/1 line
        per state per transport."""
        state_gauge, _ = _ensure_metrics()
        for s in CircuitState:
            state_gauge.labels(transport=self._name, state=s.value).set(
                1.0 if s == self._state else 0.0
            )


# Structural protocol for the transport interface. Declared
# here (not in trip_ema.notifications.transports) to avoid the
# circular import. Concrete transports satisfy this implicitly
# via their `send(address, batch) -> bool` method.
from typing import Protocol  # noqa: E402  # local; declared near use


class NotificationTransportProtocol(Protocol):
    def send(self, address: str, batch: NotificationBatch) -> bool: ...
