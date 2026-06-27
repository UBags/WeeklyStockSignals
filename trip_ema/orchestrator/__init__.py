"""V1 cycle orchestration primitives.

Building blocks for `recompute_market` (Step 6) and
`notify_subscribers` (Step 9). Each module here is a single-purpose
primitive that runs inside whatever transaction the caller has open
on its `Connection` — no module here opens its own transaction.

Public surface:

- `derive_signals` (Step 3):
    derive_signal_for_ticker_this_cycle — O(1) per ticker, compares
        the latest two stock_indicator_state rows.
    derive_signals_for_all_active_stocks — bulk version, returns a
        list ready to feed CurrentSignalsRepository.insert_many.

- `rotate_signals` (Step 4):
    rotate_current_to_historical — atomic move of current_signals
        rows into historical_signals.

- `recompute_market` (Step 6):
    recompute_market — one pass through the V1 cycle compute
        pipeline (8 stages, live mode only — backfill in Step 11).

Later steps fill out the rest of this package:

- `notify_subscribers` (Step 9): wires Step 7's composer + sender
"""
from __future__ import annotations

from .derive_signals import (
    derive_signal_for_ticker_this_cycle,
    derive_signals_for_all_active_stocks,
)
from .recompute_market import (
    RecomputeMarketResult,
    recompute_market,
)
from .rotate_signals import rotate_current_to_historical

__all__ = [
    "RecomputeMarketResult",
    "derive_signal_for_ticker_this_cycle",
    "derive_signals_for_all_active_stocks",
    "recompute_market",
    "rotate_current_to_historical",
]
