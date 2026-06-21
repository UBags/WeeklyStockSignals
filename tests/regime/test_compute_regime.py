"""Exhaustive unit tests for `trip_ema.regime.compute_regime`.

Coverage targets:
  - Each of rules (a)–(e) firing in isolation.
  - Each continuation leg firing on its own.
  - Continuation failing when neither leg holds → NEUTRAL.
  - Priority of definite rules over continuation on the same bar.
  - First-bar handling (prev_close / prev_envelope = None).
  - Warmup (NaN current-bar inputs).
  - Boundary equality (close exactly at a band).
  - Symmetry: every green case has a mirrored red case.
"""

from __future__ import annotations

import math

import pytest

from trip_ema.regime import BarInputs, Regime, compute_regime


# ---------------------------------------------------------------------------
# Bar-builder helpers — keep tests readable.
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

    Override the keyword you care about per test.
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


# ---------------------------------------------------------------------------
# Rule (a) — definite_green
# ---------------------------------------------------------------------------

class TestDefiniteGreen:
    def test_close_strictly_above_upper_inner_paints_green(self):
        assert compute_regime(
            Regime.NEUTRAL, None, None, None, _bar(close=111.0),
        ) == Regime.BULLISH

    def test_close_exactly_at_upper_inner_paints_green(self):
        """Spec uses `>=`, so equality counts."""
        assert compute_regime(
            Regime.NEUTRAL, None, None, None, _bar(close=110.0),
        ) == Regime.BULLISH

    def test_definite_green_wins_regardless_of_previous_regime(self):
        for prev in (Regime.NEUTRAL, Regime.BULLISH, Regime.BEARISH):
            assert compute_regime(
                prev, 100.0, 95.0, 105.0, _bar(close=115.0),
            ) == Regime.BULLISH, f"failed for prev={prev}"


# ---------------------------------------------------------------------------
# Rule (b) — definite_red
# ---------------------------------------------------------------------------

class TestDefiniteRed:
    def test_close_strictly_below_lower_inner_paints_red(self):
        assert compute_regime(
            Regime.NEUTRAL, None, None, None, _bar(close=89.0),
        ) == Regime.BEARISH

    def test_close_exactly_at_lower_inner_paints_red(self):
        assert compute_regime(
            Regime.NEUTRAL, None, None, None, _bar(close=90.0),
        ) == Regime.BEARISH

    def test_definite_red_wins_regardless_of_previous_regime(self):
        for prev in (Regime.NEUTRAL, Regime.BULLISH, Regime.BEARISH):
            assert compute_regime(
                prev, 100.0, 95.0, 105.0, _bar(close=85.0),
            ) == Regime.BEARISH, f"failed for prev={prev}"


# ---------------------------------------------------------------------------
# Rule (c) — green_continuation
# ---------------------------------------------------------------------------

class TestGreenContinuationLeg1:
    """Leg 1: close >= lower_envelope. The price is still inside the
    ribbon's lower bound — green continues."""

    def test_close_at_lower_envelope_continues_green(self):
        # Inside corridor (between lower_inner=90 and upper_inner=110)
        # AND at lower envelope.
        assert compute_regime(
            Regime.BULLISH,
            previous_close=100.0,
            previous_lower_envelope=95.0,
            previous_upper_envelope=105.0,
            bar=_bar(close=95.0),  # close == lower_envelope
        ) == Regime.BULLISH

    def test_close_above_lower_envelope_continues_green(self):
        assert compute_regime(
            Regime.BULLISH, 100.0, 95.0, 105.0, _bar(close=100.0),
        ) == Regime.BULLISH

    def test_leg1_does_not_apply_when_prev_was_not_green(self):
        # Same bar, prev=NEUTRAL → no continuation, falls through to NEUTRAL.
        assert compute_regime(
            Regime.NEUTRAL, 100.0, 95.0, 105.0, _bar(close=100.0),
        ) == Regime.NEUTRAL


class TestGreenContinuationLeg2:
    """Leg 2: close >= lower_inner AND prev_close >= prev_lower_envelope.
    Ribbon dipped, price held the inner band, prev close was above
    yesterday's ribbon — green continues."""

    def test_leg2_fires_when_leg1_does_not(self):
        # Today: close=92 → between lower_inner=90 and lower_envelope=95
        # → leg 1 fails (92 < 95).
        # Yesterday: prev_close=98 >= prev_lower_envelope=97 → leg 2 holds.
        assert compute_regime(
            Regime.BULLISH,
            previous_close=98.0,
            previous_lower_envelope=97.0,
            previous_upper_envelope=105.0,
            bar=_bar(close=92.0),
        ) == Regime.BULLISH

    def test_leg2_fails_when_prev_close_below_prev_lower_envelope(self):
        # Today's close above lower_inner but yesterday's close was
        # already below the ribbon → neither leg holds.
        assert compute_regime(
            Regime.BULLISH,
            previous_close=94.0,
            previous_lower_envelope=97.0,
            previous_upper_envelope=105.0,
            bar=_bar(close=92.0),
        ) == Regime.NEUTRAL

    def test_leg2_requires_close_at_or_above_lower_inner(self):
        # Today's close strictly below lower_inner → rule (b) fires
        # FIRST (definite_red), so we never reach the continuation.
        assert compute_regime(
            Regime.BULLISH,
            previous_close=98.0,
            previous_lower_envelope=97.0,
            previous_upper_envelope=105.0,
            bar=_bar(close=89.0),
        ) == Regime.BEARISH

    def test_leg2_just_above_lower_inner_continues_green(self):
        """When close == lower_inner exactly, rule (b) fires FIRST
        (definite_red uses `<=`) and we never reach the continuation.
        Leg 2 is reachable only when close is strictly above lower_inner.
        Pick close=90.1 — just clears rule (b), leg 1 still fails
        (90.1 < lower_envelope=95), leg 2 holds → GREEN."""
        assert compute_regime(
            Regime.BULLISH,
            previous_close=98.0,
            previous_lower_envelope=97.0,
            previous_upper_envelope=105.0,
            bar=_bar(close=90.1),
        ) == Regime.BULLISH


class TestGreenContinuationFailure:
    def test_both_legs_fail_drops_to_neutral(self):
        # Close below lower_envelope (leg 1 fails) AND
        # close below lower_inner — but wait, that would hit rule (b).
        # Pick close strictly between lower_inner and lower_envelope
        # so we're past (b) but leg 1 fails:
        # close=92 → above lower_inner=90 (rule b doesn't fire),
        #           below lower_envelope=95 (leg 1 fails).
        # And prev_close below prev_lower_envelope → leg 2 fails.
        assert compute_regime(
            Regime.BULLISH,
            previous_close=94.0,
            previous_lower_envelope=97.0,
            previous_upper_envelope=105.0,
            bar=_bar(close=92.0),
        ) == Regime.NEUTRAL


# ---------------------------------------------------------------------------
# Rule (d) — red_continuation. Mirror of (c).
# ---------------------------------------------------------------------------

class TestRedContinuationLeg1:
    def test_close_at_upper_envelope_continues_red(self):
        assert compute_regime(
            Regime.BEARISH,
            previous_close=100.0,
            previous_lower_envelope=95.0,
            previous_upper_envelope=105.0,
            bar=_bar(close=105.0),  # close == upper_envelope
        ) == Regime.BEARISH

    def test_close_below_upper_envelope_continues_red(self):
        assert compute_regime(
            Regime.BEARISH, 100.0, 95.0, 105.0, _bar(close=100.0),
        ) == Regime.BEARISH

    def test_leg1_does_not_apply_when_prev_was_not_red(self):
        assert compute_regime(
            Regime.NEUTRAL, 100.0, 95.0, 105.0, _bar(close=100.0),
        ) == Regime.NEUTRAL


class TestRedContinuationLeg2:
    def test_leg2_fires_when_leg1_does_not(self):
        # Today: close=108 → between upper_envelope=105 and upper_inner=110
        # → leg 1 fails (108 > 105).
        # Yesterday: prev_close=102 <= prev_upper_envelope=103 → leg 2 holds.
        assert compute_regime(
            Regime.BEARISH,
            previous_close=102.0,
            previous_lower_envelope=95.0,
            previous_upper_envelope=103.0,
            bar=_bar(close=108.0),
        ) == Regime.BEARISH

    def test_leg2_fails_when_prev_close_above_prev_upper_envelope(self):
        assert compute_regime(
            Regime.BEARISH,
            previous_close=106.0,
            previous_lower_envelope=95.0,
            previous_upper_envelope=103.0,
            bar=_bar(close=108.0),
        ) == Regime.NEUTRAL

    def test_leg2_just_below_upper_inner_continues_red(self):
        """Mirror: close == upper_inner exactly hits rule (a) first.
        Leg 2 only reachable when close is strictly below upper_inner.
        upper_envelope=103, close=109.9 → leg 1 fails (109.9 > 103),
        leg 2 prev-conditions hold → RED."""
        assert compute_regime(
            Regime.BEARISH,
            previous_close=102.0,
            previous_lower_envelope=95.0,
            previous_upper_envelope=103.0,
            bar=_bar(close=109.9),
        ) == Regime.BEARISH


class TestRedContinuationFailure:
    def test_both_legs_fail_drops_to_neutral(self):
        # close=108 → above upper_envelope=105 (leg 1 fails),
        #            below upper_inner=110 (rule a doesn't fire).
        # prev_close=106 > prev_upper_envelope=103 → leg 2 fails.
        assert compute_regime(
            Regime.BEARISH,
            previous_close=106.0,
            previous_lower_envelope=95.0,
            previous_upper_envelope=103.0,
            bar=_bar(close=108.0),
        ) == Regime.NEUTRAL


# ---------------------------------------------------------------------------
# Priority: definite rules outrank continuation
# ---------------------------------------------------------------------------

class TestPriority:
    def test_definite_green_beats_red_continuation_on_flip(self):
        """prev=RED, today's close >= upper_inner → GREEN, not red-continued.
        This is the sharp reversal case."""
        assert compute_regime(
            Regime.BEARISH,
            previous_close=92.0,
            previous_lower_envelope=95.0,
            previous_upper_envelope=105.0,
            bar=_bar(close=112.0),
        ) == Regime.BULLISH

    def test_definite_red_beats_green_continuation_on_flip(self):
        assert compute_regime(
            Regime.BULLISH,
            previous_close=108.0,
            previous_lower_envelope=95.0,
            previous_upper_envelope=105.0,
            bar=_bar(close=88.0),
        ) == Regime.BEARISH


# ---------------------------------------------------------------------------
# First bar (no previous)
# ---------------------------------------------------------------------------

class TestFirstBar:
    def test_first_bar_can_paint_green_via_rule_a(self):
        assert compute_regime(
            Regime.NEUTRAL, None, None, None, _bar(close=115.0),
        ) == Regime.BULLISH

    def test_first_bar_can_paint_red_via_rule_b(self):
        assert compute_regime(
            Regime.NEUTRAL, None, None, None, _bar(close=85.0),
        ) == Regime.BEARISH

    def test_first_bar_falls_to_neutral_when_in_corridor(self):
        # No previous → no continuation possible; price between inner
        # bands → no definite rule. Must be NEUTRAL.
        assert compute_regime(
            Regime.NEUTRAL, None, None, None, _bar(close=100.0),
        ) == Regime.NEUTRAL

    def test_continuation_cannot_fire_with_none_prev_close(self):
        # Even if prev_regime were GREEN (shouldn't happen on the first
        # bar but defensive), absent prev_close blocks leg 2; leg 1
        # uses today's lower_envelope only, so it CAN fire — assert
        # that's the only path.
        # close=100, lower_envelope=95 → leg 1 holds.
        assert compute_regime(
            Regime.BULLISH, None, None, None, _bar(close=100.0),
        ) == Regime.BULLISH
        # close=92 → leg 1 fails (92 < 95); leg 2 blocked by None.
        assert compute_regime(
            Regime.BULLISH, None, None, None, _bar(close=92.0),
        ) == Regime.NEUTRAL


# ---------------------------------------------------------------------------
# Warmup — NaN in current-bar inputs
# ---------------------------------------------------------------------------

class TestWarmup:
    def test_nan_close_returns_neutral(self):
        assert compute_regime(
            Regime.BULLISH, 100.0, 95.0, 105.0,
            _bar(close=math.nan),
        ) == Regime.NEUTRAL

    def test_nan_band_returns_neutral(self):
        assert compute_regime(
            Regime.BULLISH, 100.0, 95.0, 105.0,
            _bar(close=100.0, upper_inner=math.nan),
        ) == Regime.NEUTRAL

    def test_nan_envelope_returns_neutral(self):
        assert compute_regime(
            Regime.BULLISH, 100.0, 95.0, 105.0,
            _bar(close=100.0, lower_envelope=math.nan),
        ) == Regime.NEUTRAL

    def test_inf_inputs_also_return_neutral(self):
        # Defensive — shouldn't happen but guarded.
        assert compute_regime(
            Regime.NEUTRAL, None, None, None,
            _bar(close=math.inf),
        ) == Regime.NEUTRAL


# ---------------------------------------------------------------------------
# Sanity / regression: a few hand-rolled multi-bar walks
# ---------------------------------------------------------------------------

class TestMultiBarWalks:
    """Compose compute_regime across a small sequence; verify the
    state-threading produces the obvious answer."""

    def test_neutral_to_green_to_continuation(self):
        # Bar 1: neutral start, close=115 → GREEN.
        r1 = compute_regime(
            Regime.NEUTRAL, None, None, None, _bar(close=115.0),
        )
        assert r1 == Regime.BULLISH

        # Bar 2: close=100 (inside lower_envelope=95) → continuation, GREEN.
        r2 = compute_regime(
            r1, 115.0, 95.0, 105.0, _bar(close=100.0),
        )
        assert r2 == Regime.BULLISH

        # Bar 3: close=92 (below lower_envelope, above lower_inner) AND
        # prev_close=100 was above prev_lower_envelope=95 → leg 2 holds, GREEN.
        r3 = compute_regime(
            r2, 100.0, 95.0, 105.0, _bar(close=92.0),
        )
        assert r3 == Regime.BULLISH

        # Bar 4: close=92 again; prev_close=92 was BELOW prev_lower_envelope=95
        # → leg 2 fails; leg 1 also fails → drops to NEUTRAL.
        r4 = compute_regime(
            r3, 92.0, 95.0, 105.0, _bar(close=92.0),
        )
        assert r4 == Regime.NEUTRAL

    def test_green_to_red_sharp_reversal_paints_red_immediately(self):
        r1 = compute_regime(
            Regime.NEUTRAL, None, None, None, _bar(close=115.0),
        )
        assert r1 == Regime.BULLISH

        # Big down bar — close=85 → rule (b) fires; not red-continued
        # because there was no red state to continue. Just paints RED.
        r2 = compute_regime(
            r1, 115.0, 95.0, 105.0, _bar(close=85.0),
        )
        assert r2 == Regime.BEARISH


# ---------------------------------------------------------------------------
# Symmetry — every green case has a mirrored red case.
# ---------------------------------------------------------------------------

class TestSymmetry:
    """For each (close, prev_close, bands) triple that produces GREEN,
    its mirror around 100 should produce RED."""

    @pytest.mark.parametrize("close,prev_close,expect", [
        (115.0, None,  Regime.BULLISH),
        (100.0, 115.0, Regime.BULLISH),  # leg 1 continuation
        ( 92.0,  98.0, Regime.BULLISH),  # leg 2 continuation
        ( 92.0,  94.0, Regime.NEUTRAL),  # both continuation legs fail
    ])
    def test_mirror_around_100(self, close, prev_close, expect):
        # Helper to mirror a value around 100.
        m = lambda v: None if v is None else 200.0 - v  # noqa: E731

        # Green-side check.
        prev_lower_env = 95.0 if prev_close is not None else None
        prev_upper_env = 105.0 if prev_close is not None else None
        prev_regime = Regime.BULLISH if prev_close is not None else Regime.NEUTRAL
        green_result = compute_regime(
            prev_regime, prev_close, prev_lower_env, prev_upper_env,
            _bar(close=close),
        )
        assert green_result == expect

        # Red-side mirror.
        mirror_prev_regime = (
            Regime.BEARISH if prev_close is not None else Regime.NEUTRAL
        )
        red_result = compute_regime(
            mirror_prev_regime,
            m(prev_close),
            m(prev_upper_env),  # mirroring swaps upper<->lower
            m(prev_lower_env),
            _bar(close=m(close)),
        )
        mirror_expect = {
            Regime.BULLISH: Regime.BEARISH,
            Regime.BEARISH: Regime.BULLISH,
            Regime.NEUTRAL: Regime.NEUTRAL,
        }[expect]
        assert red_result == mirror_expect, (
            f"symmetry broken: close={close} → {green_result.name}, "
            f"mirror close={m(close)} → {red_result.name}, expected "
            f"{mirror_expect.name}"
        )
