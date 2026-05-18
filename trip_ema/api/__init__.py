"""Public callable surface — three functions.

(a) compute_full_verbose / compute_full_terse  — stateless ad-hoc compute
(b) query_cached_verbose  / query_cached_terse — fast path via SignalCache
(c) prime_all_tickers                          — startup orchestration

Plus the response types and error classes used by all three.
"""

from .cached import query_cached_terse, query_cached_verbose
from .lifecycle import LifecycleResult, prime_all_tickers
from .responses import (
    InsufficientHistoryError,
    TerseResponse,
    TickerNotPrimedError,
    VerboseResponse,
)
from .stateless import compute_full_terse, compute_full_verbose

__all__ = [
    "InsufficientHistoryError",
    "LifecycleResult",
    "TerseResponse",
    "TickerNotPrimedError",
    "VerboseResponse",
    "compute_full_terse",
    "compute_full_verbose",
    "prime_all_tickers",
    "query_cached_terse",
    "query_cached_verbose",
]
