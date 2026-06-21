"""Background scheduling: compute loop, notification distribution,
and per-market orchestration."""

from ._wall_clock import (
    seconds_until_next_local_tick,
    seconds_until_next_tick,
)
from .compute_scheduler import (
    ComputeRunSummary,
    ComputeScheduler,
)
from .market_calendar import (
    TradingCalendar,
    TradingCalendarConfig,
)
from .market_hours import MarketHoursService
from .market_orchestrator import (
    MarketOrchestrator,
    OrchestratorRunSummary,
    OrchestratorSettings,
    OrchestratorStepResult,
    PulsePublisher,
)
from .orchestrator_pool import (
    OrchestratorPool,
    build_pool_from_configs,
)
from .subscriber_distributor import DistributionSummary, SubscriberDistributor
from .tick_planner import (
    MarketTickConfig,
    compute_tick_times_for_day,
    next_tick_after,
)

__all__ = [
    # Step 1 — pure scheduling primitives
    "MarketTickConfig",
    "TradingCalendar",
    "TradingCalendarConfig",
    "compute_tick_times_for_day",
    "next_tick_after",
    "seconds_until_next_local_tick",
    "seconds_until_next_tick",
    # Step 3 — per-market orchestrator
    "MarketOrchestrator",
    "OrchestratorRunSummary",
    "OrchestratorSettings",
    "OrchestratorStepResult",
    "PulsePublisher",
    # Step 4 — pool
    "OrchestratorPool",
    "build_pool_from_configs",
    # Pre-existing
    "ComputeRunSummary",
    "ComputeScheduler",
    "DistributionSummary",
    "MarketHoursService",
    "SubscriberDistributor",
]
