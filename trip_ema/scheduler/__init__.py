"""Background scheduling: compute loop, notification distribution,
and per-market orchestration."""

# PHASE-3B: MarketOrchestrator + OrchestratorPool exports removed
# (modules deleted). Phase 3a RunScheduler + RunDispatcher are the
# scheduling subsystem now; both are imported directly by app.py
# rather than re-exported from this package's __init__.
from ._wall_clock import (
    seconds_until_next_local_tick,
    seconds_until_next_tick,
)
from .market_calendar import (
    TradingCalendar,
    TradingCalendarConfig,
)
from .market_hours import MarketHoursService
from .tick_planner import (
    MarketTickConfig,
    compute_tick_entries_for_day,
    compute_tick_times_for_day,
    next_tick_after,
)

__all__ = [
    # Pure scheduling primitives
    "MarketTickConfig",
    "TradingCalendar",
    "TradingCalendarConfig",
    "compute_tick_entries_for_day",  # Phase 3a
    "compute_tick_times_for_day",
    "next_tick_after",
    "seconds_until_next_local_tick",
    "seconds_until_next_tick",
    # Market hours service
    "MarketHoursService",
]
