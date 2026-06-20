"""Exhaustive unit tests for `trip_ema.signals.generate_signals`.

Coverage targets:
  - BUY first leg fires from every non-BUY previous state
  - BUY first leg suppressed by close > upper_outer
  - BUY second leg fires (pullback-recross recovery)
  - BUY second leg blocked by every guard (state, prev_regime,
    prev_close, close, None handling)
  - SELL mirrors of everything above
  - EXIT_FROM_BUY fires on BUY → non-green
  - EXIT_FROM_SELL fires on SELL → non-red
  - Precedence E: BUY > EXIT_FROM_SELL (SELL → BUY in one step)
  - Precedence F: SELL > EXIT_FROM_BUY (BUY → SELL in one step)
  - No-op cases (state unchanged, no signal)
  - First bar: prev_close = None
  - Warmup: current_regime = NEUTRAL
  - Multi-bar walks: realistic sequences
  - Symmetry: every green case has a mirrored red case
"""

from __future__ import annotations

import pytest

from trip_ema.domain import ChangeSignal
from trip_ema.regime import BarInputs, Regime
from trip_ema.signals import PositionState, SignalDecision, generate_signal


# ---------------------------------------------------------------------------
# Bar-builder helper — same default layout as the regime tests.
# ---------------------------------------------------------------------------

def _bar(
    close: float,
    *,
    upper_inner: float = 110.0,
    lower_inner: float = 90.0,
    upper_outer: float = 120.0,
    lower_outer: float = 80.0,
    upper_envelope: float = 105.0,
    lower_envelope: float = 95.0,
) -> BarInputs:
    """Default band layout, centred around 100.

      lower_outer=80  lower_inner=90  lower_env=95  upper_env=105  upper_inner=110  upper_outer=120
    """
    return BarInputs(
        close=close,
        upper_inner=upper_inner,
        lower_inner=lower_inner,
        upper_outer=upper_outer,
        lower_outer=lower_outer,
        upper_envelope=upper_envelope,
        lower_envelope=lower_envelope,
    )


def _expect(
    result: SignalDecision,
    state: PositionState,
    signal: ChangeSignal,
) -> None:
    """Friendlier failure message than two separate asserts."""
    assert result.new_state == state and result.signal_kind == signal, (
        f"got ({result.new_state.name}, {result.signal_kind.name}), "
        f"expected ({state.name}, {signal.name})"
    )


# ---------------------------------------------------------------------------
# Rule A — BUY first leg
# ---------------------------------------------------------------------------

class TestBuyFirstLeg:
    """First leg: prev_regime != GREEN AND current_regime == GREEN AND
    close <= upper_outer. Fresh entry into the green corridor without
    chasing a parabolic move."""

    def test_fires_from_flat_with_prev_neutral(self):
        # State=FLAT, prev=NEUTRAL → GREEN, close inside corridor.
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=100.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=115.0),  # 115 <= upper_outer=120
        )
        _expect(result, PositionState.BUY, ChangeSignal.BUY)

    def test_fires_from_flat_with_prev_red(self):
        # Sharp reversal: prev was RED, now GREEN.
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=85.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=115.0),
        )
        _expect(result, PositionState.BUY, ChangeSignal.BUY)

    def test_fires_from_sell_with_prev_red_suppresses_exit(self):
        """Precedence E: state=SELL, prev=RED, now GREEN.
        BUY fires; EXIT_FROM_SELL suppressed; state SELL → BUY directly."""
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=85.0,
            previous_state=PositionState.SELL,
            current_regime=Regime.GREEN,
            bar=_bar(close=115.0),
        )
        _expect(result, PositionState.BUY, ChangeSignal.BUY)

    def test_fires_from_sell_with_prev_neutral_suppresses_exit(self):
        """Precedence E: state=SELL, prev=NEUTRAL, now GREEN.
        BUY fires; EXIT_FROM_SELL suppressed."""
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=100.0,
            previous_state=PositionState.SELL,
            current_regime=Regime.GREEN,
            bar=_bar(close=115.0),
        )
        _expect(result, PositionState.BUY, ChangeSignal.BUY)

    def test_fires_at_upper_outer_boundary(self):
        """close == upper_outer exactly. Spec uses `<=` so this counts."""
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=100.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=120.0),  # exactly at upper_outer
        )
        _expect(result, PositionState.BUY, ChangeSignal.BUY)


class TestBuyFirstLegSuppressed:
    def test_close_above_upper_outer_blocks_first_leg(self):
        """Regime is GREEN but close blew past upper_outer.
        No signal; state stays FLAT (will be eligible for second leg)."""
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=100.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=120.01),  # just past upper_outer
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)

    def test_already_buy_state_blocks_first_leg(self):
        """state=BUY already. Don't re-emit BUY."""
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=100.0,
            previous_state=PositionState.BUY,
            current_regime=Regime.GREEN,
            bar=_bar(close=115.0),
        )
        _expect(result, PositionState.BUY, ChangeSignal.NO_CHANGE)


# ---------------------------------------------------------------------------
# Rule A — BUY second leg
# ---------------------------------------------------------------------------

class TestBuySecondLeg:
    """Second leg: prev_regime == GREEN AND current_regime == GREEN AND
    prev_close <= upper_inner AND close >= upper_inner. The pullback-
    and-recross recovery."""

    def test_fires_when_close_recrosses_upper_inner_upward(self):
        """Yesterday: regime green, close = 105 (below upper_inner=110).
        Today: regime still green, close = 111 (recrossed upward)."""
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=105.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=111.0),
        )
        _expect(result, PositionState.BUY, ChangeSignal.BUY)

    def test_fires_at_upper_inner_boundary_both_sides(self):
        """prev_close == upper_inner AND close == upper_inner —
        both use `<=` / `>=` respectively, so this is the tightest
        legal case."""
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=110.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=110.0),
        )
        _expect(result, PositionState.BUY, ChangeSignal.BUY)

    def test_fires_recovering_from_first_leg_block(self):
        """The realistic scenario this rule exists for:
          bar N-1: regime turned green, close=125 > upper_outer → first
                   leg blocked, no signal, state stayed FLAT.
          bar N:   regime still green, close pulled back to 108, prev
                   close from N-1 was 125 > upper_inner — second leg
                   would fail because prev_close > upper_inner.
          bar N+1: regime still green, prev_close = 108 <= upper_inner,
                   today close = 112 >= upper_inner → second leg fires.
        """
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=108.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=112.0),
        )
        _expect(result, PositionState.BUY, ChangeSignal.BUY)


class TestBuySecondLegBlocked:
    def test_already_buy_state_blocks_second_leg(self):
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=105.0,
            previous_state=PositionState.BUY,
            current_regime=Regime.GREEN,
            bar=_bar(close=111.0),
        )
        _expect(result, PositionState.BUY, ChangeSignal.NO_CHANGE)

    def test_prev_close_above_upper_inner_blocks(self):
        """prev_close > upper_inner means we weren't 'below' yesterday."""
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=115.0,  # already above upper_inner=110
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=116.0),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)

    def test_close_below_upper_inner_blocks(self):
        """close hasn't crossed upper_inner upward yet."""
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=105.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=109.99),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)

    def test_prev_close_none_blocks_second_leg(self):
        """Defensive: if writer passes prev_close=None even with a
        non-NEUTRAL prev_regime (shouldn't happen, but…), the second
        leg must not fire."""
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=None,
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=111.0),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)


# ---------------------------------------------------------------------------
# Rule B — SELL first leg (mirror of A)
# ---------------------------------------------------------------------------

class TestSellFirstLeg:
    def test_fires_from_flat_with_prev_neutral(self):
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=100.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.RED,
            bar=_bar(close=85.0),  # 85 >= lower_outer=80
        )
        _expect(result, PositionState.SELL, ChangeSignal.SELL)

    def test_fires_from_flat_with_prev_green(self):
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=115.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.RED,
            bar=_bar(close=85.0),
        )
        _expect(result, PositionState.SELL, ChangeSignal.SELL)

    def test_fires_from_buy_suppresses_exit_from_buy(self):
        """Precedence F: state=BUY, sharp reversal to RED.
        SELL fires; EXIT_FROM_BUY suppressed; state BUY → SELL in one step."""
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=115.0,
            previous_state=PositionState.BUY,
            current_regime=Regime.RED,
            bar=_bar(close=85.0),
        )
        _expect(result, PositionState.SELL, ChangeSignal.SELL)

    def test_fires_at_lower_outer_boundary(self):
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=100.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.RED,
            bar=_bar(close=80.0),  # exactly at lower_outer
        )
        _expect(result, PositionState.SELL, ChangeSignal.SELL)


class TestSellFirstLegSuppressed:
    def test_close_below_lower_outer_blocks_first_leg(self):
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=100.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.RED,
            bar=_bar(close=79.99),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)

    def test_already_sell_state_blocks_first_leg(self):
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=100.0,
            previous_state=PositionState.SELL,
            current_regime=Regime.RED,
            bar=_bar(close=85.0),
        )
        _expect(result, PositionState.SELL, ChangeSignal.NO_CHANGE)


# ---------------------------------------------------------------------------
# Rule B — SELL second leg (mirror)
# ---------------------------------------------------------------------------

class TestSellSecondLeg:
    def test_fires_when_close_recrosses_lower_inner_downward(self):
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=95.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.RED,
            bar=_bar(close=89.0),
        )
        _expect(result, PositionState.SELL, ChangeSignal.SELL)

    def test_fires_at_lower_inner_boundary_both_sides(self):
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=90.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.RED,
            bar=_bar(close=90.0),
        )
        _expect(result, PositionState.SELL, ChangeSignal.SELL)


class TestSellSecondLegBlocked:
    def test_already_sell_state_blocks(self):
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=95.0,
            previous_state=PositionState.SELL,
            current_regime=Regime.RED,
            bar=_bar(close=89.0),
        )
        _expect(result, PositionState.SELL, ChangeSignal.NO_CHANGE)

    def test_prev_close_below_lower_inner_blocks(self):
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=85.0,  # already below lower_inner=90
            previous_state=PositionState.FLAT,
            current_regime=Regime.RED,
            bar=_bar(close=84.0),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)

    def test_close_above_lower_inner_blocks(self):
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=95.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.RED,
            bar=_bar(close=90.01),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)

    def test_prev_close_none_blocks(self):
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=None,
            previous_state=PositionState.FLAT,
            current_regime=Regime.RED,
            bar=_bar(close=89.0),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)


# ---------------------------------------------------------------------------
# Rule C — EXIT_FROM_BUY
# ---------------------------------------------------------------------------

class TestExitFromBuy:
    def test_fires_on_buy_to_neutral(self):
        """state=BUY, regime turns NEUTRAL → EXIT_FROM_BUY, state=FLAT."""
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=115.0,
            previous_state=PositionState.BUY,
            current_regime=Regime.NEUTRAL,
            bar=_bar(close=100.0),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.EXIT_FROM_BUY)

    def test_fires_on_buy_to_red_when_sell_leg_blocked(self):
        """state=BUY, regime turns RED, but SELL first leg fails
        (close < lower_outer). EXIT_FROM_BUY fires because no SELL
        suppresses it. State → FLAT."""
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=115.0,
            previous_state=PositionState.BUY,
            current_regime=Regime.RED,
            bar=_bar(close=79.0),  # below lower_outer=80 → SELL blocked
        )
        _expect(result, PositionState.FLAT, ChangeSignal.EXIT_FROM_BUY)

    def test_does_not_fire_when_state_is_flat(self):
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=100.0,
            previous_state=PositionState.FLAT,
            current_regime=Regime.NEUTRAL,
            bar=_bar(close=100.0),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)


# ---------------------------------------------------------------------------
# Rule D — EXIT_FROM_SELL (mirror)
# ---------------------------------------------------------------------------

class TestExitFromSell:
    def test_fires_on_sell_to_neutral(self):
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=85.0,
            previous_state=PositionState.SELL,
            current_regime=Regime.NEUTRAL,
            bar=_bar(close=100.0),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.EXIT_FROM_SELL)

    def test_fires_on_sell_to_green_when_buy_leg_blocked(self):
        """state=SELL, regime turns GREEN, but BUY first leg fails
        (close > upper_outer). EXIT_FROM_SELL fires."""
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=85.0,
            previous_state=PositionState.SELL,
            current_regime=Regime.GREEN,
            bar=_bar(close=121.0),  # above upper_outer=120 → BUY blocked
        )
        _expect(result, PositionState.FLAT, ChangeSignal.EXIT_FROM_SELL)


# ---------------------------------------------------------------------------
# Precedence E and F — verified directly
# ---------------------------------------------------------------------------

class TestPrecedence:
    """Even though precedence E/F were exercised inside the BUY/SELL
    first-leg suites, also verify directly that:
      - BUY fired from SELL state → no EXIT_FROM_SELL emitted
      - SELL fired from BUY state → no EXIT_FROM_BUY emitted
      - State transitions in one step (no intermediate FLAT)
    """

    def test_e_sell_to_buy_in_one_step(self):
        result = generate_signal(
            previous_regime=Regime.RED,
            previous_close=85.0,
            previous_state=PositionState.SELL,
            current_regime=Regime.GREEN,
            bar=_bar(close=115.0),
        )
        # Critical: state goes SELL → BUY directly, signal is BUY (not
        # EXIT_FROM_SELL, not two signals).
        assert result.new_state == PositionState.BUY
        assert result.signal_kind == ChangeSignal.BUY

    def test_f_buy_to_sell_in_one_step(self):
        result = generate_signal(
            previous_regime=Regime.GREEN,
            previous_close=115.0,
            previous_state=PositionState.BUY,
            current_regime=Regime.RED,
            bar=_bar(close=85.0),
        )
        assert result.new_state == PositionState.SELL
        assert result.signal_kind == ChangeSignal.SELL


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

class TestNoOpCases:
    def test_flat_neutral_neutral_is_noop(self):
        result = generate_signal(
            Regime.NEUTRAL, 100.0, PositionState.FLAT,
            Regime.NEUTRAL, _bar(close=100.0),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)

    def test_buy_continuation_inside_corridor_is_noop(self):
        """state=BUY, prev=GREEN, current=GREEN, neither second-leg
        condition holds (close is comfortably inside)."""
        result = generate_signal(
            Regime.GREEN, 115.0, PositionState.BUY,
            Regime.GREEN, _bar(close=115.0),
        )
        _expect(result, PositionState.BUY, ChangeSignal.NO_CHANGE)

    def test_sell_continuation_inside_corridor_is_noop(self):
        result = generate_signal(
            Regime.RED, 85.0, PositionState.SELL,
            Regime.RED, _bar(close=85.0),
        )
        _expect(result, PositionState.SELL, ChangeSignal.NO_CHANGE)


# ---------------------------------------------------------------------------
# First bar — no previous data
# ---------------------------------------------------------------------------

class TestFirstBar:
    def test_first_bar_neutral_to_green_fires_buy(self):
        """Very first bar of a series: prev_regime=NEUTRAL (default),
        prev_close=None, state=FLAT. If current_regime turns green,
        first leg can still fire (it doesn't need prev_close)."""
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=None,
            previous_state=PositionState.FLAT,
            current_regime=Regime.GREEN,
            bar=_bar(close=115.0),
        )
        _expect(result, PositionState.BUY, ChangeSignal.BUY)

    def test_first_bar_neutral_to_red_fires_sell(self):
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=None,
            previous_state=PositionState.FLAT,
            current_regime=Regime.RED,
            bar=_bar(close=85.0),
        )
        _expect(result, PositionState.SELL, ChangeSignal.SELL)

    def test_first_bar_neutral_to_neutral_is_noop(self):
        result = generate_signal(
            previous_regime=Regime.NEUTRAL,
            previous_close=None,
            previous_state=PositionState.FLAT,
            current_regime=Regime.NEUTRAL,
            bar=_bar(close=100.0),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)


# ---------------------------------------------------------------------------
# Warmup — current_regime is NEUTRAL because of NaN inputs
# ---------------------------------------------------------------------------

class TestWarmup:
    """When `compute_regime` returns NEUTRAL during warmup, the signal
    generator behaves identically to "regime is genuinely neutral":
    no BUY/SELL, and EXIT only if state had been BUY/SELL.

    The writer is responsible for not putting state into BUY/SELL
    during warmup; this test just confirms the rule doesn't have a
    special warmup branch (no hidden behaviour)."""

    def test_warmup_with_flat_state_is_noop(self):
        result = generate_signal(
            Regime.NEUTRAL, None, PositionState.FLAT,
            Regime.NEUTRAL, _bar(close=100.0),
        )
        _expect(result, PositionState.FLAT, ChangeSignal.NO_CHANGE)


# ---------------------------------------------------------------------------
# Multi-bar walks — realistic sequences
# ---------------------------------------------------------------------------

class TestMultiBarWalks:
    """Compose generate_signal across short sequences. Each bar's
    output becomes the next bar's `previous_*` inputs."""

    def test_flat_to_buy_to_continuation_to_exit(self):
        # Bar 1: first BUY.
        r1 = generate_signal(
            Regime.NEUTRAL, 100.0, PositionState.FLAT,
            Regime.GREEN, _bar(close=115.0),
        )
        _expect(r1, PositionState.BUY, ChangeSignal.BUY)

        # Bar 2: still green, comfortable continuation → no signal.
        r2 = generate_signal(
            Regime.GREEN, 115.0, r1.new_state,
            Regime.GREEN, _bar(close=118.0),
        )
        _expect(r2, PositionState.BUY, ChangeSignal.NO_CHANGE)

        # Bar 3: regime drops to NEUTRAL → EXIT_FROM_BUY.
        r3 = generate_signal(
            Regime.GREEN, 118.0, r2.new_state,
            Regime.NEUTRAL, _bar(close=100.0),
        )
        _expect(r3, PositionState.FLAT, ChangeSignal.EXIT_FROM_BUY)

    def test_first_leg_blocked_then_second_leg_recovers(self):
        """The realistic recovery scenario rule A's second leg exists for.

        bar 1: NEUTRAL → GREEN, close=125 > upper_outer=120 →
               first leg blocked, state stays FLAT.
        bar 2: still GREEN (regime continues), close pulls back to 108.
               Second leg: prev_close=125 NOT <= upper_inner=110 → blocked.
        bar 3: still GREEN, close=112 >= upper_inner. Now prev_close=108
               IS <= upper_inner → second leg fires → BUY.
        """
        r1 = generate_signal(
            Regime.NEUTRAL, 100.0, PositionState.FLAT,
            Regime.GREEN, _bar(close=125.0),
        )
        _expect(r1, PositionState.FLAT, ChangeSignal.NO_CHANGE)

        r2 = generate_signal(
            Regime.GREEN, 125.0, r1.new_state,
            Regime.GREEN, _bar(close=108.0),
        )
        _expect(r2, PositionState.FLAT, ChangeSignal.NO_CHANGE)

        r3 = generate_signal(
            Regime.GREEN, 108.0, r2.new_state,
            Regime.GREEN, _bar(close=112.0),
        )
        _expect(r3, PositionState.BUY, ChangeSignal.BUY)

    def test_sharp_reversal_buy_to_sell(self):
        # Bar 1: enter BUY.
        r1 = generate_signal(
            Regime.NEUTRAL, 100.0, PositionState.FLAT,
            Regime.GREEN, _bar(close=115.0),
        )
        _expect(r1, PositionState.BUY, ChangeSignal.BUY)

        # Bar 2: massive down bar → RED, close inside SELL corridor.
        # Precedence F: SELL emitted, EXIT_FROM_BUY suppressed.
        r2 = generate_signal(
            Regime.GREEN, 115.0, r1.new_state,
            Regime.RED, _bar(close=85.0),
        )
        _expect(r2, PositionState.SELL, ChangeSignal.SELL)


# ---------------------------------------------------------------------------
# Symmetry — mirror green-side cases around 100, expect red-side answers
# ---------------------------------------------------------------------------

class TestSymmetry:
    """Confirm the BUY and SELL rules are exact mirrors. Any
    asymmetry here would be a bug."""

    @pytest.mark.parametrize(
        "prev_regime,prev_close,prev_state,current_regime,close,expect_state,expect_signal",
        [
            # Green-side cases: (we'll mirror them in the test body)
            (Regime.NEUTRAL, 100.0, PositionState.FLAT, Regime.GREEN, 115.0,
             PositionState.BUY,  ChangeSignal.BUY),
            (Regime.NEUTRAL, 100.0, PositionState.FLAT, Regime.GREEN, 120.01,
             PositionState.FLAT, ChangeSignal.NO_CHANGE),
            (Regime.GREEN,   105.0, PositionState.FLAT, Regime.GREEN, 111.0,
             PositionState.BUY,  ChangeSignal.BUY),
            (Regime.GREEN,   115.0, PositionState.BUY,  Regime.NEUTRAL, 100.0,
             PositionState.FLAT, ChangeSignal.EXIT_FROM_BUY),
        ],
    )
    def test_mirror_around_100(
        self, prev_regime, prev_close, prev_state, current_regime,
        close, expect_state, expect_signal,
    ):
        # Green-side first — sanity check our input.
        green_result = generate_signal(
            prev_regime, prev_close, prev_state, current_regime, _bar(close=close),
        )
        _expect(green_result, expect_state, expect_signal)

        # Mirror around 100: bands are symmetric around 100 by default,
        # so we mirror close, prev_close, swap GREEN↔RED, swap BUY↔SELL.
        mirror_close = 200.0 - close
        mirror_prev_close = None if prev_close is None else 200.0 - prev_close
        regime_mirror = {
            Regime.GREEN: Regime.RED,
            Regime.RED: Regime.GREEN,
            Regime.NEUTRAL: Regime.NEUTRAL,
        }
        state_mirror = {
            PositionState.BUY: PositionState.SELL,
            PositionState.SELL: PositionState.BUY,
            PositionState.FLAT: PositionState.FLAT,
        }
        signal_mirror = {
            ChangeSignal.BUY: ChangeSignal.SELL,
            ChangeSignal.SELL: ChangeSignal.BUY,
            ChangeSignal.EXIT_FROM_BUY: ChangeSignal.EXIT_FROM_SELL,
            ChangeSignal.EXIT_FROM_SELL: ChangeSignal.EXIT_FROM_BUY,
            ChangeSignal.NO_CHANGE: ChangeSignal.NO_CHANGE,
        }

        red_result = generate_signal(
            regime_mirror[prev_regime],
            mirror_prev_close,
            state_mirror[prev_state],
            regime_mirror[current_regime],
            _bar(close=mirror_close),
        )
        _expect(red_result, state_mirror[expect_state], signal_mirror[expect_signal])
