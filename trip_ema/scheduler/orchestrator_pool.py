"""OrchestratorPool — owns N MarketOrchestrators and their lifecycle.

One pool per app instance. Constructed at startup from the list of
`MarketHoursConfig` entries in `config.market_hours`, plus the shared
dependencies (db engine, compute scheduler, distributor, pulse state).
The pool encapsulates:

  * Building one MarketOrchestrator per market.
  * `start_all()` / `stop_all()` for clean lifecycle management from
    `app.py`'s startup/shutdown hooks.
  * `get(exchange)` lookup for admin endpoints that target a specific
    market.
  * `statuses()` for the unified status payload.
  * Publishing `orchestrator_started` / `orchestrator_stopped` events
    to PulseState so the GUI gets per-market lifecycle visibility.

Concurrency model
-----------------
The pool itself does NOT spawn threads. Each MarketOrchestrator has
its own daemon thread for its tick loop. The pool just owns
references and dispatches lifecycle calls. Methods that walk the
orchestrator list are thread-safe via `_lock`.

What is NOT here
----------------
* The factory function that wires up the real fetch/compute/distribute
  callables from the runtime dependencies — that's in `app.py` (Step 6).
  The pool takes pre-built orchestrators or a factory; testable either way.
* HTTP endpoint logic — Step 5.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable, Optional, Protocol

from .market_orchestrator import MarketOrchestrator

logger = logging.getLogger(__name__)


# Forward Protocol type to avoid importing the concrete PulseState
# class (which lives in the transports.http package). The pool only
# needs the two service-level event methods; full PulseState is fine
# but a thinner duck-typed interface is cleaner here.
class _PoolPulseHook(Protocol):
    """The pool calls these on lifecycle transitions; PulseState
    implements them structurally. Tests can pass a Mock that supplies
    the two methods."""
    def publish_orchestrator_started(self, exchange: str) -> None: ...
    def publish_orchestrator_stopped(
        self, exchange: str, *, reason: str = "normal",
    ) -> None: ...


class OrchestratorPool:
    """Container + lifecycle manager for per-market orchestrators."""

    def __init__(
        self,
        orchestrators: Iterable[MarketOrchestrator],
        *,
        pulse_hook: Optional[_PoolPulseHook] = None,
    ):
        """Construct a pool from pre-built orchestrators.

        `pulse_hook`: optional PulseState (or anything with the two
        `publish_orchestrator_*` methods). When set, lifecycle events
        are broadcast to subscribers.
        """
        self._lock = threading.Lock()
        self._pulse_hook = pulse_hook

        # Build the per-exchange map. Reject duplicates here rather
        # than silently overwriting — a duplicate is a config bug.
        self._by_exchange: dict[str, MarketOrchestrator] = {}
        for orch in orchestrators:
            ex = orch.exchange
            if ex in self._by_exchange:
                raise ValueError(
                    f"Duplicate exchange in orchestrator pool: {ex!r}. "
                    f"Check market_hours config for repeated entries."
                )
            self._by_exchange[ex] = orch

    # ---- Lifecycle ----

    def start_all(self) -> None:
        """Start every orchestrator's loop thread.

        Called from app startup. Failures starting one orchestrator do
        NOT abort the others — partial-start is better than nothing
        when one market's calendar/config is broken.
        """
        with self._lock:
            orchestrators = list(self._by_exchange.values())

        for orch in orchestrators:
            try:
                orch.start()
                if self._pulse_hook is not None:
                    self._pulse_hook.publish_orchestrator_started(
                        orch.exchange,
                    )
            except Exception:
                logger.exception(
                    "OrchestratorPool: failed to start orchestrator for %s; "
                    "continuing with others",
                    orch.exchange,
                )

    def stop_all(self, timeout_per_orchestrator: float = 30.0) -> None:
        """Stop every orchestrator. Bounded; if one orchestrator's loop
        is wedged, we give up on it after `timeout_per_orchestrator`
        seconds and move on to the next."""
        with self._lock:
            orchestrators = list(self._by_exchange.values())

        for orch in orchestrators:
            try:
                orch.stop(timeout=timeout_per_orchestrator)
                if self._pulse_hook is not None:
                    self._pulse_hook.publish_orchestrator_stopped(
                        orch.exchange, reason="normal",
                    )
            except Exception:
                logger.exception(
                    "OrchestratorPool: failed to stop %s cleanly; continuing",
                    orch.exchange,
                )

    # ---- Lookups for HTTP endpoints ----

    def get(self, exchange: str) -> Optional[MarketOrchestrator]:
        """Return the orchestrator for `exchange`, or None if unknown.
        Case-sensitive: matches the config exchange string verbatim."""
        with self._lock:
            return self._by_exchange.get(exchange)

    def exchanges(self) -> list[str]:
        """Return the sorted list of managed exchange codes."""
        with self._lock:
            return sorted(self._by_exchange.keys())

    def all(self) -> list[MarketOrchestrator]:
        """All orchestrators, sorted by exchange code."""
        with self._lock:
            return [self._by_exchange[ex] for ex in sorted(self._by_exchange)]

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_exchange)

    def __contains__(self, exchange: str) -> bool:
        with self._lock:
            return exchange in self._by_exchange

    # ---- Health surface ----

    def any_unhealthy(self) -> bool:
        """True if any orchestrator was started but its thread has died.
        Mirrors `MarketOrchestrator.is_unhealthy()` aggregated. Suitable
        for `/readyz` to report degraded service."""
        for orch in self.all():
            if orch.is_unhealthy():
                return True
        return False


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def build_pool_from_configs(
    *,
    market_configs: list,  # list of MarketHoursConfig
    settings: Any,  # OrchestratorSettings
    fetch_fn_factory: Callable[[str], Callable],
    compute_run_once: Callable,
    distribute_run_once: Callable,
    pulse: Optional[_PoolPulseHook] = None,
) -> "OrchestratorPool":
    """Construct an OrchestratorPool from runtime dependencies.

    Used by app.py's startup wiring (Step 6). Tests construct pools
    directly with mocked orchestrators instead of going through here.

    `market_configs`: list of MarketHoursConfig (from Config.market_hours).
        Each is converted to a (MarketTickConfig, TradingCalendar) pair
        via the Step 2 adapter methods.

    `fetch_fn_factory(exchange)` returns the fetch callable for that
        exchange. The factory pattern lets each orchestrator have its
        own bound function without sharing mutable state.

    `compute_run_once` and `distribute_run_once` are shared across all
        markets — they're singletons in the process (single compute
        scheduler + single distributor). The reentrancy locks inside
        those objects handle multi-market concurrency.
    """
    # Lazy import to keep this module's import graph small.
    from .market_orchestrator import MarketOrchestrator  # noqa: PLC0415
    from .market_calendar import TradingCalendar  # noqa: PLC0415

    orchestrators = []
    for mc in market_configs:
        tick_config = mc.to_tick_config()
        calendar_config = mc.to_calendar_config()
        calendar = TradingCalendar(calendar_config)
        orch = MarketOrchestrator(
            market=tick_config,
            calendar=calendar,
            settings=settings,
            fetch_fn=fetch_fn_factory(tick_config.exchange),
            compute_run_once=compute_run_once,
            distribute_run_once=distribute_run_once,
            pulse=pulse,  # The same PulseState instance for everyone.
        )
        orchestrators.append(orch)

    return OrchestratorPool(orchestrators, pulse_hook=pulse)
