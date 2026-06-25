"""Signal event computation (Phase 6-1).

Pure function: given a time-ordered list of IndicatorState rows
for one series, emit the SignalEvent rows that fired.

Four signal kinds, all derived from display_regime transitions:

    BUY            : display_regime went from non-+1 to +1
    SELL           : display_regime went from non--1 to -1
    EXIT_FROM_BUY  : display_regime went from +1 to non-+1
    EXIT_FROM_SELL : display_regime went from -1 to non--1

Matches the operator's Pine Script:
    longCondition      = display == 1 and prev != 1   → BUY
    exitLongCondition  = prev == 1 and display != 1   → EXIT_FROM_BUY
    shortCondition     = display == -1 and prev != -1 → SELL
    exitShortCondition = prev == -1 and display != -1 → EXIT_FROM_SELL

Precedence (operator override of Pine semantics):
  BUY / SELL always WIN over EXIT_FROM_BUY / EXIT_FROM_SELL on
  the same bar. When display_regime jumps directly +1 → -1 or
  -1 → +1, only the new-side OPEN event fires; the opposite-side
  EXIT is suppressed.

  Rationale: a regime flip is, semantically, a single decision —
  'now flat-and-short' or 'now flat-and-long'. Emitting both an
  EXIT and an OPEN for the same flip muddies downstream views
  (Last 5 signals, email notifications) without adding any
  information the OPEN alone doesn't carry.

  Single-direction exits (regime → 0) are unaffected —
  EXIT_FROM_BUY / EXIT_FROM_SELL still fire alone when leaving a
  position to flat.

Example of suppression:
  regime +1 → -1  : SELL fires; EXIT_FROM_BUY suppressed.
  regime -1 → +1  : BUY fires;  EXIT_FROM_SELL suppressed.
  regime +1 →  0  : EXIT_FROM_BUY fires (no opposite-side OPEN).
  regime -1 →  0  : EXIT_FROM_SELL fires (no opposite-side OPEN).

The first bar's `prev` is treated as 0 (nz() default in Pine).
This means a bar where display_regime is +1 on the very first bar
of the series produces a BUY event with regime_before=0.

Why close_at_event is passed in via callable:
  IndicatorState doesn't store the underlying close/value. The
  caller supplies a lookup so events get the REAL price at the
  event bar, not an EMA-derived proxy. For industry indices this
  comes from industry_index_value; for tickers it'd come from
  ohlcv_bars.
"""
from __future__ import annotations

from datetime import datetime, UTC
from collections.abc import Callable, Iterable

from . import SignalEvent


def compute_signal_events_from_states(
    states: Iterable,
    close_lookup: Callable,
) -> list:
    """Walk `states` (a time-ordered iterable of IndicatorState
    rows) and emit a list of SignalEvent rows.

    Args:
      states: time-ordered iterable of IndicatorState rows, sorted
        oldest-first. Passing newest-first inverts all transitions
        and produces wrong events.
      close_lookup: callable that takes a trade_date and returns
        the underlying close/value as float. For industries, this
        wraps a dict built from industry_index_value rows.

    Returns a list of SignalEvent rows, ordered by trade_date.

    The function emits events bar-by-bar with no lookahead, so
    streaming use (computing today's events from yesterday's
    state + today's state) works the same as bulk use.
    """
    events: list = []
    prev_display = 0  # nz() default in Pine
    now = datetime.now(UTC)

    for state in states:
        curr = state.display_regime
        close_value = float(close_lookup(state.trade_date))

        # Detect which signals NATURALLY fire on this bar from the
        # regime transition, before applying precedence suppression.
        buy_fires       = (curr == 1 and prev_display != 1)
        sell_fires      = (curr == -1 and prev_display != -1)
        exit_buy_fires  = (prev_display == 1 and curr != 1)
        exit_sell_fires = (prev_display == -1 and curr != -1)

        # Precedence: a direct +1 ↔ -1 flip emits only the new-side
        # OPEN; the opposite-side EXIT is suppressed.
        #   +1 → -1 : SELL wins  (EXIT_FROM_BUY suppressed)
        #   -1 → +1 : BUY wins   (EXIT_FROM_SELL suppressed)
        # Single-side exits (regime → 0) are untouched.
        if buy_fires and exit_sell_fires:
            exit_sell_fires = False
        if sell_fires and exit_buy_fires:
            exit_buy_fires = False

        if buy_fires:
            events.append(SignalEvent(
                ticker_id=state.ticker_id,
                timeframe=state.timeframe,
                trade_date=state.trade_date,
                event_kind="BUY",
                regime_before=prev_display,
                regime_after=curr,
                close_at_event=close_value,
                computed_at=now,
            ))
        if sell_fires:
            events.append(SignalEvent(
                ticker_id=state.ticker_id,
                timeframe=state.timeframe,
                trade_date=state.trade_date,
                event_kind="SELL",
                regime_before=prev_display,
                regime_after=curr,
                close_at_event=close_value,
                computed_at=now,
            ))
        if exit_buy_fires:
            events.append(SignalEvent(
                ticker_id=state.ticker_id,
                timeframe=state.timeframe,
                trade_date=state.trade_date,
                event_kind="EXIT_FROM_BUY",
                regime_before=prev_display,
                regime_after=curr,
                close_at_event=close_value,
                computed_at=now,
            ))
        if exit_sell_fires:
            events.append(SignalEvent(
                ticker_id=state.ticker_id,
                timeframe=state.timeframe,
                trade_date=state.trade_date,
                event_kind="EXIT_FROM_SELL",
                regime_before=prev_display,
                regime_after=curr,
                close_at_event=close_value,
                computed_at=now,
            ))
        prev_display = curr

    return events
