"""Pure signal generator — the single canonical implementation.

Given (previous_regime, previous_close, previous_state, current_regime,
current_bar_inputs), return the new position state and the signal kind
that fired on this bar.

No I/O. No state. No imports outside `domain/` and `regime/`. This
function is the only file in the codebase that implements the signal
rules — every other consumer either calls this directly or reads the
persisted `signal_events` rows it produces.

The companion file `regime/compute_regime.py` decides what color a bar
is. This file decides what trading-state event (if any) that color
implies, given the previous bar's state.

Threading: stateless and idempotent. Safe to call concurrently.

Evaluation order (top wins on a tie — though by construction BUY and
SELL cannot both fire on the same bar since `current_regime` can't be
both GREEN and RED):

  1. Check BUY  (rule A).         If fires → emit BUY,  state := BUY.
  2. Check SELL (rule B).         If fires → emit SELL, state := SELL.
  3. If state was BUY  and current_regime != GREEN → emit EXIT_FROM_BUY,
     state := FLAT.
  4. If state was SELL and current_regime != RED  → emit EXIT_FROM_SELL,
     state := FLAT.
  5. Otherwise: emit NO_CHANGE, state unchanged.

When BUY fires from a previous SELL state, the implicit EXIT_FROM_SELL
is suppressed (precedence E): state transitions SELL → BUY in one step,
no intermediate FLAT. Mirror for SELL fired from a previous BUY state
(precedence F).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from trip_ema.domain import ChangeSignal
from trip_ema.regime import BarInputs, Regime


class PositionState(IntEnum):
    """Mutually exclusive trading-position state per (ticker, timeframe).

    Threaded forward bar-by-bar by the writer (signals/generate_and_-
    backfill_signals.py and signals/generate_signals_in_live_production.py).
    The pure function never persists state — the writer does.

    Numeric values match the sign convention of `Regime` and
    `ChangeSignal`: BUY/long-side positive, SELL/short-side negative,
    neutral zero.
    """

    FLAT = 0
    BUY = 1
    SELL = -1


@dataclass(frozen=True, slots=True)
class SignalDecision:
    """The output of `generate_signal`.

    `signal_kind == ChangeSignal.NO_CHANGE` means no signal fires on
    this bar — the writer does not insert into `signal_events`. The
    `new_state` field still carries the (possibly unchanged) state to
    thread to the next bar.
    """

    new_state: PositionState
    signal_kind: ChangeSignal


def generate_signal(
    previous_regime: Regime,
    previous_close: float | None,
    previous_state: PositionState,
    current_regime: Regime,
    bar: BarInputs,
) -> SignalDecision:
    """Determine the signal fired by this bar and the resulting position state.

    Pure. Idempotent. No I/O.

    Args:
        previous_regime: Regime of the previous bar (from `compute_regime`).
            On the very first bar of a series, pass `Regime.NEUTRAL`.
        previous_close: Close of the previous bar. `None` on the very
            first bar of a series. The second leg of rules A/B is
            gated on this being non-None.
        previous_state: Position state at the end of the previous bar.
            Starts at `PositionState.FLAT` for a fresh series.
        current_regime: Regime of this bar (already computed by
            `compute_regime`). The writer is responsible for calling
            `compute_regime` first and passing the result here.
        bar: Current bar's inputs (close + four band values + two
            envelope values). Only `close`, `upper_inner`, `lower_inner`,
            `upper_outer`, `lower_outer` are read by this function;
            the envelope fields are accepted for type-shape symmetry
            with `compute_regime` and ignored here.

    Returns:
        `SignalDecision(new_state, signal_kind)`. `signal_kind` is
        one of `NO_CHANGE`, `BUY`, `SELL`, `EXIT_FROM_BUY`,
        `EXIT_FROM_SELL`. `new_state` is the state to thread to the
        next bar.
    """
    # ---- Rule A: BUY ------------------------------------------------ #
    # Fires iff we're not already BUY, this bar is green, and either:
    #   leg 1 — prev was non-green AND close is at or below the upper
    #           outer band (the "don't chase a parabolic blow-off" guard);
    #   leg 2 — prev was green, prev_close was at or below upper_inner,
    #           today's close has recrossed upper_inner upward (the
    #           pullback-and-recross recovery for a leg-1 that was
    #           suppressed by the outer-band guard).
    buy_fires = (
        previous_state != PositionState.BUY
        and current_regime == Regime.GREEN
        and (
            (
                previous_regime != Regime.GREEN
                and bar.close <= bar.upper_outer
            )
            or (
                previous_regime == Regime.GREEN
                and previous_close is not None
                and previous_close <= bar.upper_inner
                and bar.close >= bar.upper_inner
            )
        )
    )

    # ---- Rule B: SELL (mirror of A) -------------------------------- #
    sell_fires = (
        previous_state != PositionState.SELL
        and current_regime == Regime.RED
        and (
            (
                previous_regime != Regime.RED
                and bar.close >= bar.lower_outer
            )
            or (
                previous_regime == Regime.RED
                and previous_close is not None
                and previous_close >= bar.lower_inner
                and bar.close <= bar.lower_inner
            )
        )
    )

    # ---- Precedence E: BUY > EXIT_FROM_SELL ------------------------ #
    # When prev_state was SELL and BUY fires, state goes SELL → BUY
    # in one step. The implicit EXIT_FROM_SELL is not emitted.
    if buy_fires:
        return SignalDecision(PositionState.BUY, ChangeSignal.BUY)

    # ---- Precedence F: SELL > EXIT_FROM_BUY ------------------------ #
    if sell_fires:
        return SignalDecision(PositionState.SELL, ChangeSignal.SELL)

    # ---- Rule C: EXIT_FROM_BUY ------------------------------------- #
    # Reached only when no BUY/SELL fired this bar.
    if (
        previous_state == PositionState.BUY
        and current_regime != Regime.GREEN
    ):
        return SignalDecision(PositionState.FLAT, ChangeSignal.EXIT_FROM_BUY)

    # ---- Rule D: EXIT_FROM_SELL ------------------------------------ #
    if (
        previous_state == PositionState.SELL
        and current_regime != Regime.RED
    ):
        return SignalDecision(PositionState.FLAT, ChangeSignal.EXIT_FROM_SELL)

    # ---- No signal — state unchanged ------------------------------- #
    return SignalDecision(previous_state, ChangeSignal.NO_CHANGE)
