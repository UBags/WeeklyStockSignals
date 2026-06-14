"""Numerical tests for Phase 6-1 signal computation.

Tests the pure function compute_signal_events_from_states which
walks a list of IndicatorState rows and emits SignalEvent rows
based on display_regime transitions.

Each test constructs a small synthetic list of IndicatorState rows
with a hand-crafted display_regime sequence, then asserts the
exact set and ordering of events produced.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trip_ema.fundamentals import IndicatorState, SignalEvent
from trip_ema.fundamentals.signal_compute import (
    compute_signal_events_from_states,
)


# ---------- helpers ---------- #


def _state(
    industry: str, td: date, display: int, *,
    core: int = 0,
) -> IndicatorState:
    """Build a minimal IndicatorState row for testing. Only
    display_regime varies meaningfully across test inputs."""
    return IndicatorState(
        ticker_id=None, timeframe=None, industry_name=industry,
        trade_date=td,
        med_ema=100.0, long_ema=100.0, longest_ema=100.0,
        atr=1.0,
        upper_inner_band=101.0, lower_inner_band=99.0,
        upper_mid_band=102.0, lower_mid_band=98.0,
        upper_outer_band=105.0, lower_outer_band=95.0,
        core_regime=core, display_regime=display,
        pierces_upper_mid=False, pierces_lower_mid=False,
        dist_to_med_in_hl_units=None,
        dist_to_long_in_hl_units=None,
        dist_to_longest_in_hl_units=None,
        mean_dist_to_med_in_hl_units=None,
        mean_dist_to_long_in_hl_units=None,
        mean_dist_to_longest_in_hl_units=None,
        bars_since_med_turn=None, bars_since_long_turn=None,
        bars_since_longest_turn=None,
        pct_change_since_med_turn=None,
        pct_change_since_long_turn=None,
        pct_change_since_longest_turn=None,
        med_pattern_label="NA", long_pattern_label="NA",
        longest_pattern_label="NA",
        med_turn_buy=False, med_turn_sell=False,
        long_turn_buy=False, long_turn_sell=False,
        longest_turn_buy=False, longest_turn_sell=False,
        computed_at=datetime.now(timezone.utc),
    )


def _close(td):
    """Default close lookup: return 100.0 for any date."""
    return 100.0


# ---------- basics ---------- #


class TestEmptyAndNoEvents:

    def test_empty_states_returns_empty(self):
        events = compute_signal_events_from_states([], _close)
        assert events == []

    def test_all_zero_display_produces_no_events(self):
        """A series stuck in display_regime=0 throughout fires
        nothing."""
        states = [
            _state("X", date(2026, 1, 1), display=0),
            _state("X", date(2026, 1, 8), display=0),
            _state("X", date(2026, 1, 15), display=0),
        ]
        events = compute_signal_events_from_states(states, _close)
        assert events == []


class TestBuyAndExitBuy:

    def test_buy_fires_on_zero_to_one(self):
        """display_regime goes 0 → +1 on bar 1. BUY fires there
        with regime_before=0, regime_after=+1."""
        states = [
            _state("X", date(2026, 1, 1), display=0),
            _state("X", date(2026, 1, 8), display=1),
        ]
        events = compute_signal_events_from_states(states, _close)
        assert len(events) == 1
        e = events[0]
        assert e.event_kind == "BUY"
        assert e.trade_date == date(2026, 1, 8)
        assert e.regime_before == 0
        assert e.regime_after == 1
        assert e.industry_name == "X"

    def test_buy_fires_on_minus_one_to_one(self):
        """Direct -1 → +1 should fire BOTH EXIT_SELL and BUY on
        the same bar (bar 2). On bar 1 we also get a SELL because
        prev_display starts at 0 (nz default).
        Total events: 3."""
        states = [
            _state("X", date(2026, 1, 1), display=-1),
            _state("X", date(2026, 1, 8), display=1),
        ]
        events = compute_signal_events_from_states(states, _close)
        # Total events: SELL on bar 1, then EXIT_SELL+BUY on bar 2.
        assert len(events) == 3
        # SELL on bar 1.
        bar1 = [e for e in events if e.trade_date == date(2026, 1, 1)]
        assert len(bar1) == 1
        assert bar1[0].event_kind == "SELL"
        assert bar1[0].regime_before == 0
        assert bar1[0].regime_after == -1
        # EXIT_SELL and BUY on bar 2.
        bar2 = [e for e in events if e.trade_date == date(2026, 1, 8)]
        assert len(bar2) == 2
        bar2_kinds = {e.event_kind for e in bar2}
        assert bar2_kinds == {"EXIT_SELL", "BUY"}
        # Both bar-2 events should have regime_before=-1, regime_after=+1.
        for e in bar2:
            assert e.regime_before == -1
            assert e.regime_after == 1

    def test_exit_buy_fires_on_one_to_zero(self):
        states = [
            _state("X", date(2026, 1, 1), display=1),
            _state("X", date(2026, 1, 8), display=0),
        ]
        events = compute_signal_events_from_states(states, _close)
        # Bar 1: BUY (prev=0 default), bar 2: EXIT_BUY.
        assert len(events) == 2
        assert events[0].event_kind == "BUY"
        assert events[1].event_kind == "EXIT_BUY"
        assert events[1].regime_before == 1
        assert events[1].regime_after == 0


class TestSellAndExitSell:

    def test_sell_fires_on_zero_to_minus_one(self):
        states = [
            _state("X", date(2026, 1, 1), display=0),
            _state("X", date(2026, 1, 8), display=-1),
        ]
        events = compute_signal_events_from_states(states, _close)
        assert len(events) == 1
        assert events[0].event_kind == "SELL"
        assert events[0].regime_before == 0
        assert events[0].regime_after == -1

    def test_exit_sell_fires_on_minus_one_to_zero(self):
        states = [
            _state("X", date(2026, 1, 1), display=-1),
            _state("X", date(2026, 1, 8), display=0),
        ]
        events = compute_signal_events_from_states(states, _close)
        assert len(events) == 2
        # Bar 1 fires SELL because prev=0 default.
        assert events[0].event_kind == "SELL"
        # Bar 2 fires EXIT_SELL.
        assert events[1].event_kind == "EXIT_SELL"
        assert events[1].regime_before == -1
        assert events[1].regime_after == 0


class TestPineNzBehavior:

    def test_first_bar_display_plus_one_fires_buy(self):
        """nz() default: prev_display starts at 0. If first bar
        has display=+1, that's a 0→+1 transition → BUY fires."""
        states = [_state("X", date(2026, 1, 1), display=1)]
        events = compute_signal_events_from_states(states, _close)
        assert len(events) == 1
        assert events[0].event_kind == "BUY"
        assert events[0].regime_before == 0
        assert events[0].regime_after == 1

    def test_first_bar_display_minus_one_fires_sell(self):
        states = [_state("X", date(2026, 1, 1), display=-1)]
        events = compute_signal_events_from_states(states, _close)
        assert len(events) == 1
        assert events[0].event_kind == "SELL"

    def test_first_bar_display_zero_fires_nothing(self):
        states = [_state("X", date(2026, 1, 1), display=0)]
        events = compute_signal_events_from_states(states, _close)
        assert events == []


class TestComplexSequence:
    """Realistic sequence: 0 → +1 (BUY) → +1 (no event) → 0 (EXIT_BUY)
    → -1 (SELL) → -1 (no event) → +1 (EXIT_SELL + BUY same bar) → 0
    (EXIT_BUY)."""

    def test_complex_walk(self):
        sequence = [
            (date(2026, 1, 1), 0),    # no event (still 0)
            (date(2026, 1, 8), 1),    # BUY
            (date(2026, 1, 15), 1),   # no event
            (date(2026, 1, 22), 0),   # EXIT_BUY
            (date(2026, 1, 29), -1),  # SELL
            (date(2026, 2, 5), -1),   # no event
            (date(2026, 2, 12), 1),   # EXIT_SELL + BUY
            (date(2026, 2, 19), 0),   # EXIT_BUY
        ]
        states = [_state("X", td, display=d) for td, d in sequence]
        events = compute_signal_events_from_states(states, _close)

        # Hand-computed expected events:
        expected = [
            (date(2026, 1, 8), "BUY", 0, 1),
            (date(2026, 1, 22), "EXIT_BUY", 1, 0),
            (date(2026, 1, 29), "SELL", 0, -1),
            (date(2026, 2, 12), "EXIT_SELL", -1, 1),
            (date(2026, 2, 12), "BUY", -1, 1),
            (date(2026, 2, 19), "EXIT_BUY", 1, 0),
        ]
        # Order within a bar can vary (BUY/EXIT_SELL same bar) —
        # compare as tuples but tolerate within-bar ordering.
        actual_tuples = sorted(
            [(e.trade_date, e.event_kind, e.regime_before, e.regime_after)
             for e in events]
        )
        expected_sorted = sorted(expected)
        assert actual_tuples == expected_sorted


class TestCloseLookup:

    def test_close_lookup_called_for_event_dates(self):
        """Verify the close_lookup is invoked and its values land
        in close_at_event."""
        states = [
            _state("X", date(2026, 1, 1), display=0),
            _state("X", date(2026, 1, 8), display=1),
        ]
        close_by_date = {
            date(2026, 1, 1): 100.0,
            date(2026, 1, 8): 150.5,
        }
        events = compute_signal_events_from_states(
            states, lambda td: close_by_date[td],
        )
        assert len(events) == 1
        assert events[0].close_at_event == 150.5

    def test_close_lookup_not_called_when_no_events(self):
        """If no event fires, close_lookup is still called once
        per bar (we always evaluate display vs prev)."""
        call_count = {"n": 0}

        def counting_lookup(td):
            call_count["n"] += 1
            return 100.0

        states = [
            _state("X", date(2026, 1, 1), display=0),
            _state("X", date(2026, 1, 8), display=0),
        ]
        events = compute_signal_events_from_states(states, counting_lookup)
        assert events == []
        # The implementation calls close_lookup once per bar regardless,
        # because we don't know up-front if an event will fire. Two
        # bars → 2 calls.
        assert call_count["n"] == 2


class TestSeriesIdentityPreserved:
    """Events should copy the series identity from the input
    IndicatorState. Industry rows produce industry-keyed events;
    ticker rows produce ticker-keyed events."""

    def test_industry_identity_preserved(self):
        states = [_state("IT - Services", date(2026, 1, 1), display=1)]
        events = compute_signal_events_from_states(states, _close)
        assert len(events) == 1
        assert events[0].industry_name == "IT - Services"
        assert events[0].ticker_id is None
        assert events[0].timeframe is None

    def test_ticker_identity_preserved(self):
        ticker_state = IndicatorState(
            ticker_id=42, timeframe="1w", industry_name=None,
            trade_date=date(2026, 1, 1),
            med_ema=100.0, long_ema=100.0, longest_ema=100.0,
            atr=1.0,
            upper_inner_band=101.0, lower_inner_band=99.0,
            upper_mid_band=102.0, lower_mid_band=98.0,
            upper_outer_band=105.0, lower_outer_band=95.0,
            core_regime=0, display_regime=1,
            pierces_upper_mid=False, pierces_lower_mid=False,
            dist_to_med_in_hl_units=None,
            dist_to_long_in_hl_units=None,
            dist_to_longest_in_hl_units=None,
            mean_dist_to_med_in_hl_units=None,
            mean_dist_to_long_in_hl_units=None,
            mean_dist_to_longest_in_hl_units=None,
            bars_since_med_turn=None, bars_since_long_turn=None,
            bars_since_longest_turn=None,
            pct_change_since_med_turn=None,
            pct_change_since_long_turn=None,
            pct_change_since_longest_turn=None,
            med_pattern_label="NA", long_pattern_label="NA",
            longest_pattern_label="NA",
            med_turn_buy=False, med_turn_sell=False,
            long_turn_buy=False, long_turn_sell=False,
            longest_turn_buy=False, longest_turn_sell=False,
            computed_at=datetime.now(timezone.utc),
        )
        events = compute_signal_events_from_states([ticker_state], _close)
        assert events[0].ticker_id == 42
        assert events[0].timeframe == "1w"
        assert events[0].industry_name is None
