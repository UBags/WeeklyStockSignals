"""Pure signal generator + shared I/O layer + backfill + live orchestrators.

Four public surfaces:

  - **Pure rules** (`generate_signal`, `PositionState`, `SignalDecision`)
    — no I/O. The single canonical implementation of the signal rules
    A/B/C/D/E/F.

  - **Persistence** (`persist_signal_events`, `SignalCacheWriter`) — the
    shared I/O layer wrapping `SignalsRepository.insert_event` with
    the per-event loop, optional cache write-through, and the explicit
    `computed_at` policy contract.

  - **Backfill** (`run_backfill_for_ticker`, `walk_and_generate_events`,
    `BackfillResult`) — the day-by-day forward-walk orchestrator that
    regenerates historical SignalEvents. Pure walker + I/O shell.

  - **Live** (`generate_live_signal_for_ticker`, `LiveTickResult`) —
    the single-tick orchestrator the ComputeScheduler invokes once
    per tick per ticker. Same rules + same persistence as backfill;
    differs only in iteration shape (one current bar, not a walk)
    and `computed_at` policy (NOW(), not simulated day).

Rules (per spec):
  A) BUY   = NOT state_is_BUY  AND current_regime_is_green AND (
                (NOT prev_regime_is_green AND close <= upper_outer)
             OR (    prev_regime_is_green AND prev_close <= upper_inner
                                          AND      close >= upper_inner))
  B) SELL  = mirror of (A) — swap green↔red, upper↔lower, ≤↔≥
  C) EXIT_FROM_BUY  = state_is_BUY  AND NOT current_regime_is_green
  D) EXIT_FROM_SELL = state_is_SELL AND NOT current_regime_is_red
  E) BUY  takes precedence over EXIT_FROM_SELL  (SELL→BUY  in one step)
  F) SELL takes precedence over EXIT_FROM_BUY   (BUY →SELL in one step)
"""

# Step 11b2: legacy backfill imports removed —
# trip_ema.backfill.pipeline replaces generate_and_backfill_signals.
from trip_ema.signals.generate_signals import (
    PositionState,
    SignalDecision,
    generate_signal,
)
from trip_ema.signals.generate_signals_in_live_production import (
    LiveTickResult,
    generate_live_signal_for_ticker,
)
from trip_ema.signals.persist_signals import (
    SignalCacheWriter,
    persist_signal_events,
)

__all__ = [
    # Pure rules
    "PositionState",
    "SignalDecision",
    "generate_signal",
    # Persistence
    "SignalCacheWriter",
    "persist_signal_events",
    # Backfill — V1 backfill is in trip_ema.backfill.pipeline;
    # the legacy generate_and_backfill_signals module was removed
    # in Step 11b2.
    # Live
    "LiveTickResult",
    "generate_live_signal_for_ticker",
]
