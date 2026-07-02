"""Public callable surface.

  compute_full_verbose / compute_full_terse — stateless ad-hoc compute.
  Plus the response types and error classes used by both.

PHASE-3E: prime_cache_from_db + PrimeCacheResult exports removed;
lifecycle.py was deleted alongside the legacy SignalCache.
"""

from .responses import (
    InsufficientHistoryError,
    TerseResponse,
    TickerNotPrimedError,
    VerboseResponse,
)
from .stateless import compute_full_terse, compute_full_verbose

__all__ = [
    "InsufficientHistoryError",
    "TerseResponse",
    "TickerNotPrimedError",
    "VerboseResponse",
    "compute_full_terse",
    "compute_full_verbose",
]
