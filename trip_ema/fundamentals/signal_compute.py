"""Signal event computation (Phase 6-1).

Pure function: given a time-ordered list of IndicatorState rows
for one series, emit the SignalEvent rows that fired.

Four signal kinds, all derived from display_regime transitions:

    BUY       : display_regime went from non-+1 to +1
    SELL      : display_regime went from non--1 to -1
    EXIT_BUY  : display_regime went from +1 to non-+1
    EXIT_SELL : display_regime went from -1 to non--1

Matches the operator's Pine Script:
    longCondition      = display == 1 and prev != 1   → BUY
    exitLongCondition  = prev == 1 and display != 1   → EXIT_BUY
    shortCondition     = display == -1 and prev != -1 → SELL
    exitShortCondition = prev == -1 and display != -1 → EXIT_SELL

Single-bar transitions can produce TWO events on the same bar.
Example: display goes from +1 to -1 directly:
    EXIT_BUY (prev=+1, after=-1)
    SELL    (prev=+1, after=-1)
Both fire. This matches Pine — both conditionals evaluate
independently on the same bar.

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

from datetime import datetime, timezone
from typing import Callable, Iterable

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
    now = datetime.now(timezone.utc)

    for state in states:
        curr = state.display_regime
        close_value = float(close_lookup(state.trade_date))

        # Determine which (if any) of the 4 signals fired on this
        # bar. Up to 2 events can fire on one bar when display
        # goes directly from +1 to -1 or vice versa.
        if curr == 1 and prev_display != 1:
            events.append(SignalEvent(
                ticker_id=state.ticker_id,
                timeframe=state.timeframe,
                industry_name=state.industry_name,
                trade_date=state.trade_date,
                event_kind="BUY",
                regime_before=prev_display,
                regime_after=curr,
                close_at_event=close_value,
                computed_at=now,
            ))
        if curr == -1 and prev_display != -1:
            events.append(SignalEvent(
                ticker_id=state.ticker_id,
                timeframe=state.timeframe,
                industry_name=state.industry_name,
                trade_date=state.trade_date,
                event_kind="SELL",
                regime_before=prev_display,
                regime_after=curr,
                close_at_event=close_value,
                computed_at=now,
            ))
        if prev_display == 1 and curr != 1:
            events.append(SignalEvent(
                ticker_id=state.ticker_id,
                timeframe=state.timeframe,
                industry_name=state.industry_name,
                trade_date=state.trade_date,
                event_kind="EXIT_BUY",
                regime_before=prev_display,
                regime_after=curr,
                close_at_event=close_value,
                computed_at=now,
            ))
        if prev_display == -1 and curr != -1:
            events.append(SignalEvent(
                ticker_id=state.ticker_id,
                timeframe=state.timeframe,
                industry_name=state.industry_name,
                trade_date=state.trade_date,
                event_kind="EXIT_SELL",
                regime_before=prev_display,
                regime_after=curr,
                close_at_event=close_value,
                computed_at=now,
            ))
        prev_display = curr

    return events
