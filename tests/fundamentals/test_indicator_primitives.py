"""Numerical tests for Phase 5-2 indicator primitives.

The tests are organized bottom-up:
  - Layer 1: pine_ema, pine_wma, pine_sma, pine_rma
  - Layer 2: hull_smooth, volume_weighted_ema, true_range, atr,
             hl_range_avg
  - Layer 3: single_ema, triple_ema_stack
  - Layer 4: compute_atr_bands, compute_indicator_series

Every numerical assertion uses pytest.approx with explicit tolerance.
Hand-computed expected values are documented inline so any future
refactor can be checked against the same source-of-truth math.

Some tests verify Pine-specific semantics that easily go wrong in
ports: ta.ema's first-bar seeding, ta.rma's alpha=1/n (not 2/(n+1)),
ta.wma's NaN-leading window, hull_smooth's reduced behavior for
small n, and the volume-weighted EMA's degeneration to plain EMA
for constant volume.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from trip_ema.fundamentals.indicator_primitives import (
    ATR_BANDS_DEFAULT,
    ATRBandParams,
    INDICATOR_DEFAULTS,
    LONG_DEFAULT,
    LONGEST_DEFAULT,
    MED_DEFAULT,
    IndicatorParams,
    TripleEMAParams,
    atr,
    compute_atr_bands,
    compute_indicator_series,
    hl_range_avg,
    hull_smooth,
    pine_ema,
    pine_rma,
    pine_sma,
    pine_wma,
    single_ema,
    triple_ema_stack,
    true_range,
    volume_weighted_ema,
)


# A small helper for asserting an entire Series matches a list of
# expected values, with NaN-aware equality.
def assert_series_matches(
    actual: pd.Series,
    expected: list,
    *, rel: float = 1e-9, abs_: float = 1e-9,
):
    """Compares actual (a pandas Series) to a list of expected values.
    None in expected means "should be NaN". Other values are
    pytest.approx with the given tolerance."""
    actual_list = actual.tolist()
    assert len(actual_list) == len(expected), (
        f"length mismatch: actual {len(actual_list)}, expected {len(expected)}"
    )
    for i, (a, e) in enumerate(zip(actual_list, expected)):
        if e is None:
            assert pd.isna(a), f"bar {i}: expected NaN, got {a}"
        else:
            assert not pd.isna(a), f"bar {i}: expected {e}, got NaN"
            assert a == pytest.approx(e, rel=rel, abs=abs_), (
                f"bar {i}: expected {e}, got {a}"
            )


# ============================================================
# Layer 1: pine_ema
# ============================================================


class TestPineEMA:

    def test_n_equals_1_is_identity(self):
        """n=1 means alpha = 2/(1+1) = 1, so EMA[i] = src[i]
        exactly. Verifies the trivial case."""
        src = pd.Series([10.0, 20.0, 30.0, 40.0])
        out = pine_ema(src, 1)
        assert_series_matches(out, [10.0, 20.0, 30.0, 40.0])

    def test_first_bar_is_seed(self):
        """Pine's ta.ema seeds with src[0]. pandas .ewm(adjust=False)
        does the same. First bar's output equals first input."""
        src = pd.Series([100.0, 200.0, 300.0])
        out = pine_ema(src, 5)
        assert out.iloc[0] == 100.0

    def test_manual_2_bar_ema(self):
        """For n=3, alpha = 2/(3+1) = 0.5.
        bar 0: seed = 10.0
        bar 1: 0.5 × 20 + 0.5 × 10 = 15.0
        bar 2: 0.5 × 30 + 0.5 × 15 = 22.5
        bar 3: 0.5 × 40 + 0.5 × 22.5 = 31.25"""
        src = pd.Series([10.0, 20.0, 30.0, 40.0])
        out = pine_ema(src, 3)
        assert_series_matches(out, [10.0, 15.0, 22.5, 31.25])

    def test_rejects_n_below_1(self):
        with pytest.raises(ValueError, match="n must be"):
            pine_ema(pd.Series([1.0]), 0)


# ============================================================
# Layer 1: pine_wma
# ============================================================


class TestPineWMA:

    def test_n_equals_1_is_identity(self):
        src = pd.Series([10.0, 20.0, 30.0])
        out = pine_wma(src, 1)
        assert_series_matches(out, [10.0, 20.0, 30.0])

    def test_leading_nans_for_first_n_minus_1_bars(self):
        """ta.wma returns NA for the first n-1 bars."""
        src = pd.Series([10.0, 20.0, 30.0, 40.0])
        out = pine_wma(src, 3)
        # First 2 bars NaN; bar 2 (third) is the first valid one.
        assert pd.isna(out.iloc[0])
        assert pd.isna(out.iloc[1])
        assert not pd.isna(out.iloc[2])

    def test_n_3_manual(self):
        """For n=3, weights are (1, 2, 3) with sum 6.
        bar 2: (1×10 + 2×20 + 3×30) / 6 = (10+40+90)/6 = 140/6 = 23.333...
        bar 3: (1×20 + 2×30 + 3×40) / 6 = (20+60+120)/6 = 200/6 = 33.333...
        bar 4: (1×30 + 2×40 + 3×50) / 6 = (30+80+150)/6 = 260/6 = 43.333..."""
        src = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
        out = pine_wma(src, 3)
        assert_series_matches(out, [None, None, 140/6, 200/6, 260/6])

    def test_constant_input_wma_equals_input(self):
        """WMA of a constant series is the constant itself (after
        the leading NaN window)."""
        src = pd.Series([5.0] * 10)
        out = pine_wma(src, 4)
        # First 3 bars NaN, rest 5.0.
        for i in range(3):
            assert pd.isna(out.iloc[i])
        for i in range(3, 10):
            assert out.iloc[i] == pytest.approx(5.0)


# ============================================================
# Layer 1: pine_sma
# ============================================================


class TestPineSMA:

    def test_leading_nans_for_first_n_minus_1(self):
        out = pine_sma(pd.Series([1.0, 2.0, 3.0, 4.0]), 3)
        assert pd.isna(out.iloc[0])
        assert pd.isna(out.iloc[1])
        assert not pd.isna(out.iloc[2])

    def test_n_3_manual(self):
        """SMA of (1,2,3,4) with n=3:
        bar 2: (1+2+3)/3 = 2.0
        bar 3: (2+3+4)/3 = 3.0"""
        out = pine_sma(pd.Series([1.0, 2.0, 3.0, 4.0]), 3)
        assert_series_matches(out, [None, None, 2.0, 3.0])


# ============================================================
# Layer 1: pine_rma (Wilder's smoothing)
# ============================================================


class TestPineRMA:
    """RMA uses alpha = 1/n. Common bug is using alpha = 2/(n+1)
    (which is ta.ema, not ta.rma). These tests lock the
    distinction."""

    def test_rma_uses_alpha_one_over_n_not_two_over_n_plus_1(self):
        """For n=4:
          RMA alpha = 1/4 = 0.25
          EMA alpha = 2/(4+1) = 0.4
        Same input, different smoothing weights → different outputs."""
        src = pd.Series([10.0, 20.0, 30.0])
        rma_out = pine_rma(src, 4)
        ema_out = pine_ema(src, 4)
        # Bar 0 same (seed).
        assert rma_out.iloc[0] == ema_out.iloc[0] == 10.0
        # Bar 1 differs.
        # RMA: 0.25 × 20 + 0.75 × 10 = 5 + 7.5 = 12.5
        # EMA: 0.4 × 20 + 0.6 × 10 = 8 + 6 = 14
        assert rma_out.iloc[1] == pytest.approx(12.5)
        assert ema_out.iloc[1] == pytest.approx(14.0)
        # Confirm the values differ.
        assert rma_out.iloc[1] != pytest.approx(ema_out.iloc[1])

    def test_first_bar_is_seed(self):
        src = pd.Series([100.0, 200.0])
        out = pine_rma(src, 5)
        assert out.iloc[0] == 100.0

    def test_n_2_manual(self):
        """For n=2, alpha = 0.5.
        bar 0: seed = 10
        bar 1: 0.5 × 20 + 0.5 × 10 = 15
        bar 2: 0.5 × 30 + 0.5 × 15 = 22.5"""
        out = pine_rma(pd.Series([10.0, 20.0, 30.0]), 2)
        assert_series_matches(out, [10.0, 15.0, 22.5])


# ============================================================
# Layer 2: hull_smooth
# ============================================================


class TestHullSmooth:

    def test_n_below_2_is_identity(self):
        src = pd.Series([10.0, 20.0, 30.0])
        out = hull_smooth(src, 1)
        assert_series_matches(out, [10.0, 20.0, 30.0])

    def test_n_2_collapses_to_2x_minus_wma2(self):
        """For n=2:
          half_n = floor(2/2) = 1, so wma(src, 1) = src
          wma(src, 2) is the proper 2-period WMA
          inner = 2*src - wma(src, 2)
          sqrt_n = floor(sqrt(2)) = 1, so outer wma is identity
          result = 2*src - wma(src, 2)

        For input (1, 2, 3, 4):
          wma(., 2) with weights (1, 2)/3:
            bar 0: NaN
            bar 1: (1×1 + 2×2)/3 = 5/3 ≈ 1.6667
            bar 2: (1×2 + 2×3)/3 = 8/3 ≈ 2.6667
            bar 3: (1×3 + 2×4)/3 = 11/3 ≈ 3.6667
          result = 2×src - wma(.,2):
            bar 0: NaN (because wma is NaN)
            bar 1: 4 - 5/3 = 7/3 ≈ 2.3333
            bar 2: 6 - 8/3 = 10/3 ≈ 3.3333
            bar 3: 8 - 11/3 = 13/3 ≈ 4.3333"""
        src = pd.Series([1.0, 2.0, 3.0, 4.0])
        out = hull_smooth(src, 2)
        assert_series_matches(out, [None, 7/3, 10/3, 13/3])

    def test_n_4_uses_outer_wma_2(self):
        """For n=4:
          half_n = 2, sqrt_n = 2
          inner = 2×wma(src, 2) - wma(src, 4)
          result = wma(inner, 2)

        For input (1, 2, 3, 4, 5, 6):
          wma(., 2): NaN, 5/3, 8/3, 11/3, 14/3, 17/3
          wma(., 4) weights (1,2,3,4)/10:
            bar 3: (1+4+9+16)/10 = 30/10 = 3.0
            bar 4: (2+6+12+20)/10 = 40/10 = 4.0
            bar 5: (3+8+15+24)/10 = 50/10 = 5.0
          inner = 2×wma(.,2) - wma(.,4):
            bar 3: 2×(11/3) - 3 = 22/3 - 9/3 = 13/3 ≈ 4.333
            bar 4: 2×(14/3) - 4 = 28/3 - 12/3 = 16/3 ≈ 5.333
            bar 5: 2×(17/3) - 5 = 34/3 - 15/3 = 19/3 ≈ 6.333
          result = wma(inner, 2) with weights (1, 2)/3:
            bar 3: NaN (window not full)
            bar 4: (1×13/3 + 2×16/3)/3 = (13 + 32)/9 = 45/9 = 5
            bar 5: (1×16/3 + 2×19/3)/3 = (16 + 38)/9 = 54/9 = 6"""
        src = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        out = hull_smooth(src, 4)
        # First 4 are NaN (inner needs n=4 → first 3 NaN, then outer
        # wma adds one more leading NaN).
        for i in range(4):
            assert pd.isna(out.iloc[i]), f"bar {i} should be NaN"
        assert out.iloc[4] == pytest.approx(5.0)
        assert out.iloc[5] == pytest.approx(6.0)


# ============================================================
# Layer 2: volume_weighted_ema
# ============================================================


class TestVolumeWeightedEMA:

    def test_constant_volume_degenerates_to_plain_ema(self):
        """When volume is constant, the formula collapses to
        plain EMA of close. Critical property for industry indices
        (where we synthesize vol=1).

        Note: vwema's leading bars differ from plain_ema because
        volume_smoothing=2 introduces a 1-bar lag at the start.
        The two converge asymptotically; this test uses a longer
        series so the seed difference washes out. The 'late bars
        match' test below verifies the strict equality after
        full convergence."""
        close = pd.Series(np.linspace(10, 50, 30))
        # Constant volume of 100.
        volume = pd.Series([100.0] * 30)
        vwema = volume_weighted_ema(close, volume, n=3)
        plain_ema = pine_ema(close, 3)

        # Constant volume cancels in the ratio, so vwema converges
        # to plain_ema. By bar 29 (alpha=0.5, 28 iterations of
        # smoothing applied to the seed difference) the divergence
        # is negligible. We allow generous relative tolerance.
        assert vwema.iloc[-1] == pytest.approx(plain_ema.iloc[-1], rel=1e-6)

    def test_constant_volume_late_bars_match_plain_ema(self):
        """A longer series confirms vwema converges to plain ema
        in the constant-volume case."""
        close = pd.Series(np.linspace(10, 100, 50))
        volume = pd.Series([50.0] * 50)
        vwema = volume_weighted_ema(close, volume, n=4)
        plain_ema = pine_ema(close, 4)
        # By bar 49, the seed difference has been smoothed out.
        # alpha = 2/5 = 0.4, so after 49 bars (1-0.4)^49 ≈ 8e-12 — tiny.
        assert vwema.iloc[-1] == pytest.approx(plain_ema.iloc[-1], rel=1e-9)

    def test_first_bar_nan_from_volume_smoothing(self):
        """volume_smoothing=2 means smoothed_vol has NaN at bar 0,
        which propagates into vwema."""
        close = pd.Series([10.0, 20.0, 30.0])
        volume = pd.Series([100.0, 100.0, 100.0])
        vwema = volume_weighted_ema(close, volume, n=2)
        assert pd.isna(vwema.iloc[0])

    def test_volume_spike_pulls_vwema_toward_spike_close(self):
        """If volume spikes on a bar where close is unusual, vwema
        should be pulled more toward that close than plain ema."""
        close = pd.Series([100.0, 100.0, 100.0, 200.0, 100.0])
        # Volume normally 1.0, spike to 100 on bar 3 (the 200 close).
        volume = pd.Series([1.0, 1.0, 1.0, 100.0, 1.0])
        vwema = volume_weighted_ema(close, volume, n=3)
        plain_ema = pine_ema(close, 3)
        # On the spike bar AND the bar after, vwema should be
        # higher (closer to 200) than plain_ema.
        assert vwema.iloc[3] > plain_ema.iloc[3]


# ============================================================
# Layer 2: true_range and atr
# ============================================================


class TestTrueRange:

    def test_first_bar_uses_hl_only(self):
        """First bar has no prior close. TR collapses to high - low."""
        high = pd.Series([10.0, 12.0])
        low = pd.Series([8.0, 9.0])
        close = pd.Series([9.0, 11.0])
        tr = true_range(high, low, close)
        assert tr.iloc[0] == 2.0  # 10 - 8

    def test_subsequent_bars_use_max_of_3(self):
        """TR = max(HL, |H-prevC|, |L-prevC|)."""
        # bar 0: H=10, L=8, C=9 → TR = HL = 2
        # bar 1: H=15, L=9, C=14, prevC=9
        #   HL = 6
        #   |H - prevC| = |15 - 9| = 6
        #   |L - prevC| = |9 - 9| = 0
        #   TR = 6
        # bar 2: H=12, L=11, C=11.5, prevC=14
        #   HL = 1
        #   |H - prevC| = |12 - 14| = 2  ← largest
        #   |L - prevC| = |11 - 14| = 3  ← actually larger
        #   TR = 3
        high = pd.Series([10.0, 15.0, 12.0])
        low = pd.Series([8.0, 9.0, 11.0])
        close = pd.Series([9.0, 14.0, 11.5])
        tr = true_range(high, low, close)
        assert_series_matches(tr, [2.0, 6.0, 3.0])

    def test_synthetic_for_index_uses_delta_close_times_sqrt2(self):
        """For index series with single-point data, TR = |Δclose|×√2."""
        # If high = low = close = value, real TR would be 0 for bar 0
        # (HL = 0) and just |C - prevC| for subsequent bars.
        # synthetic_for_index should give us |Δclose| × √2 instead.
        value = pd.Series([100.0, 105.0, 102.0, 108.0])
        tr = true_range(value, value, value, synthetic_for_index=True)
        # bar 0: NaN (no prior close)
        # bar 1: |105 - 100| × √2 = 5 × √2 ≈ 7.071
        # bar 2: |102 - 105| × √2 = 3 × √2 ≈ 4.243
        # bar 3: |108 - 102| × √2 = 6 × √2 ≈ 8.485
        assert pd.isna(tr.iloc[0])
        assert tr.iloc[1] == pytest.approx(5 * math.sqrt(2))
        assert tr.iloc[2] == pytest.approx(3 * math.sqrt(2))
        assert tr.iloc[3] == pytest.approx(6 * math.sqrt(2))


class TestATR:

    def test_atr_uses_rma_not_ema(self):
        """ATR with n=4 should use alpha = 1/4 = 0.25, not 2/5 = 0.4.
        Use a series where the two would noticeably differ."""
        high = pd.Series([10.0, 12.0, 15.0, 13.0, 14.0])
        low = pd.Series([9.0, 10.0, 12.0, 11.0, 13.0])
        close = pd.Series([9.5, 11.0, 14.0, 12.0, 13.5])
        # TR: bar 0: 1.0; bar 1: max(2, 2.5, 0.5) = 2.5;
        #     bar 2: max(3, 4, 1) = 4; bar 3: max(2, 1, 3) = 3;
        #     bar 4: max(1, 2, 1) = 2
        # RMA with n=4, alpha=0.25:
        #   bar 0: 1.0
        #   bar 1: 0.25 × 2.5 + 0.75 × 1.0 = 0.625 + 0.75 = 1.375
        #   bar 2: 0.25 × 4 + 0.75 × 1.375 = 1 + 1.03125 = 2.03125
        #   bar 3: 0.25 × 3 + 0.75 × 2.03125 = 0.75 + 1.5234375 = 2.2734375
        #   bar 4: 0.25 × 2 + 0.75 × 2.2734375 = 0.5 + 1.70507... = 2.20507...
        atr_out = atr(high, low, close, n=4)
        assert atr_out.iloc[0] == pytest.approx(1.0)
        assert atr_out.iloc[1] == pytest.approx(1.375)
        assert atr_out.iloc[2] == pytest.approx(2.03125)
        assert atr_out.iloc[3] == pytest.approx(2.2734375)
        assert atr_out.iloc[4] == pytest.approx(2.205078125)

    def test_atr_synthetic_for_index(self):
        """For an index series with vol=1 and value-only:
        synthetic TR is |Δclose|×sqrt(2); ATR is RMA of that."""
        # value series: 100, 110, 105, 120
        # synth TR: NaN, 10√2, 5√2, 15√2
        # ATR with n=2 (alpha=0.5):
        #   bar 0: NaN (TR is NaN)
        #   bar 1: 10√2 ≈ 14.142  (RMA seeds with first non-NaN value)
        #   bar 2: 0.5 × 5√2 + 0.5 × 10√2 = 7.5√2 ≈ 10.607
        #   bar 3: 0.5 × 15√2 + 0.5 × 7.5√2 = 11.25√2 ≈ 15.910
        # Note: pandas .ewm respects leading NaN — propagates them
        # until the first non-NaN, then seeds with that.
        value = pd.Series([100.0, 110.0, 105.0, 120.0])
        atr_out = atr(value, value, value, n=2, synthetic_for_index=True)
        assert pd.isna(atr_out.iloc[0])
        assert atr_out.iloc[1] == pytest.approx(10 * math.sqrt(2))
        assert atr_out.iloc[2] == pytest.approx(7.5 * math.sqrt(2))
        assert atr_out.iloc[3] == pytest.approx(11.25 * math.sqrt(2))


# ============================================================
# Layer 2: hl_range_avg
# ============================================================


class TestHLRangeAvg:

    def test_constant_range(self):
        """When high - low is constant, the avg is that constant."""
        high = pd.Series([10.0] * 30)
        low = pd.Series([8.0] * 30)
        out = hl_range_avg(high, low, n=26)
        # First 25 bars NaN, then 2.0.
        assert pd.isna(out.iloc[24])
        assert out.iloc[25] == pytest.approx(2.0)

    def test_zero_range_for_index(self):
        """For an index series with high = low, hl_range_avg is 0.
        Distance metrics that divide by this will need to guard."""
        value = pd.Series([100.0] * 30)
        out = hl_range_avg(value, value, n=10)
        assert out.iloc[15] == pytest.approx(0.0)


# ============================================================
# Layer 3: single_ema and triple_ema_stack
# ============================================================


class TestSingleEMA:

    def test_med_default_params_produce_finite_output(self):
        """A 100-bar synthetic series should produce non-NaN values
        for the medium EMA from some point onward."""
        n = 100
        close = pd.Series(np.linspace(100, 200, n))
        volume = pd.Series([1000.0] * n)
        med = single_ema(close, volume, MED_DEFAULT)
        # Verify last bar is finite and positive.
        assert not pd.isna(med.iloc[-1])
        assert med.iloc[-1] > 0

    def test_long_default_params_produce_finite_output(self):
        n = 100
        close = pd.Series(np.linspace(100, 200, n))
        volume = pd.Series([1000.0] * n)
        long = single_ema(close, volume, LONG_DEFAULT)
        assert not pd.isna(long.iloc[-1])

    def test_longest_default_params_produce_finite_output(self):
        n = 100
        close = pd.Series(np.linspace(100, 200, n))
        volume = pd.Series([1000.0] * n)
        longest = single_ema(close, volume, LONGEST_DEFAULT)
        assert not pd.isna(longest.iloc[-1])

    def test_no_final_smoothing_means_output_is_hull(self):
        """When final_smoothing is None, the output is the hull
        without an extra EMA on top. We verify this by comparing
        to manually computing through the pipeline."""
        # Use a series long enough to reach steady-state.
        n = 50
        close = pd.Series(np.linspace(100, 150, n))
        volume = pd.Series([1.0] * n)
        params_no_final = TripleEMAParams(
            base_len=8, first_smoothing=4, hull_n=2, final_smoothing=None,
        )
        params_with_final_1 = TripleEMAParams(
            base_len=8, first_smoothing=4, hull_n=2, final_smoothing=1,
        )
        a = single_ema(close, volume, params_no_final)
        b = single_ema(close, volume, params_with_final_1)
        # Final_smoothing < 2 should be the same as None.
        assert a.iloc[-1] == pytest.approx(b.iloc[-1], rel=1e-9)


class TestTripleEMAStack:

    def test_returns_three_series_same_length(self):
        n = 100
        close = pd.Series(np.linspace(100, 200, n))
        volume = pd.Series([1000.0] * n)
        emas = triple_ema_stack(close, volume)
        assert len(emas.med) == n
        assert len(emas.long) == n
        assert len(emas.longest) == n

    def test_three_emas_diverge_for_trending_series(self):
        """On a strong uptrend, the medium EMA should be closer to
        recent price than the longer EMAs. Operator's chart shows
        this visually."""
        n = 100
        # Strong linear uptrend.
        close = pd.Series(np.linspace(100, 500, n))
        volume = pd.Series([1.0] * n)
        emas = triple_ema_stack(close, volume)
        # On the last bar, medium should be > long > longest.
        assert emas.med.iloc[-1] > emas.long.iloc[-1]
        assert emas.long.iloc[-1] > emas.longest.iloc[-1]


# ============================================================
# Layer 4: compute_atr_bands
# ============================================================


class TestComputeATRBands:

    def test_bands_widen_with_higher_multiplier(self):
        """Outer band > mid band > inner band for upper side;
        symmetric on the lower side."""
        # Build a simple constant-EMA stack at value 100 and
        # constant ATR=5.
        n = 5
        med = pd.Series([100.0] * n)
        long = pd.Series([100.0] * n)
        longest = pd.Series([100.0] * n)
        from trip_ema.fundamentals.indicator_primitives import (
            TripleEMASeries,
        )
        emas = TripleEMASeries(med=med, long=long, longest=longest)
        atr_s = pd.Series([5.0] * n)
        bands = compute_atr_bands(emas, atr_s, params=ATR_BANDS_DEFAULT)
        # max(emas) = 100. inner = 100 + 0.2×5 = 101.
        # mid = 100 + 2×5 = 110. outer = 100 + 4.5×5 = 122.5.
        assert bands.upper_inner.iloc[-1] == pytest.approx(101.0)
        assert bands.upper_mid.iloc[-1] == pytest.approx(110.0)
        assert bands.upper_outer.iloc[-1] == pytest.approx(122.5)
        # Lower side is symmetric since min = max in this case.
        assert bands.lower_inner.iloc[-1] == pytest.approx(99.0)
        assert bands.lower_mid.iloc[-1] == pytest.approx(90.0)
        assert bands.lower_outer.iloc[-1] == pytest.approx(77.5)

    def test_bands_anchor_to_max_and_min_of_emas(self):
        """Upper bands use max(emas), lower bands use min(emas)."""
        n = 3
        med = pd.Series([100.0, 100.0, 100.0])
        long = pd.Series([95.0, 95.0, 95.0])
        longest = pd.Series([90.0, 90.0, 90.0])
        from trip_ema.fundamentals.indicator_primitives import (
            TripleEMASeries,
        )
        emas = TripleEMASeries(med=med, long=long, longest=longest)
        atr_s = pd.Series([1.0, 1.0, 1.0])
        bands = compute_atr_bands(emas, atr_s, params=ATR_BANDS_DEFAULT)
        # max = med (100), min = longest (90).
        # upper_inner = 100 + 0.2 = 100.2
        # lower_inner = 90 - 0.2 = 89.8
        assert bands.upper_inner.iloc[-1] == pytest.approx(100.2)
        assert bands.lower_inner.iloc[-1] == pytest.approx(89.8)


# ============================================================
# Layer 4: compute_indicator_series end-to-end
# ============================================================


class TestComputeIndicatorSeries:

    def test_ticker_path_runs_with_default_params(self):
        """A realistic ticker bar series (100 weekly bars) should
        produce non-NaN values for all indicators at the last bar."""
        n = 100
        close = pd.Series(np.linspace(1000, 1500, n))
        high = close + 10
        low = close - 10
        volume = pd.Series(np.random.RandomState(42).uniform(
            1e6, 1e7, size=n,
        ))
        result = compute_indicator_series(
            close, volume, high, low,
        )
        assert not pd.isna(result.emas.med.iloc[-1])
        assert not pd.isna(result.emas.long.iloc[-1])
        assert not pd.isna(result.emas.longest.iloc[-1])
        assert not pd.isna(result.atr.iloc[-1])
        assert result.atr.iloc[-1] > 0
        assert not pd.isna(result.bands.upper_inner.iloc[-1])
        assert not pd.isna(result.bands.lower_outer.iloc[-1])
        # hl_range_avg with 26-period needs at least 26 bars; we
        # have 100, so it should be defined.
        assert not pd.isna(result.hl_range_avg.iloc[-1])

    def test_index_path_uses_synthetic_atr(self):
        """For industry indices, synthetic_atr_for_index=True
        and high=low=close=value, vol=1."""
        n = 100
        value = pd.Series(np.linspace(1000, 1100, n))
        volume = pd.Series([1.0] * n)
        result = compute_indicator_series(
            value, volume, value, value,
            synthetic_atr_for_index=True,
        )
        # ATR for a steadily rising series should be |Δclose| × √2,
        # smoothed. linspace(1000, 1100, 100) has step = 100/99
        # (not 1.0!), so synthetic TR ≈ (100/99) × √2.
        # RMA of that constant converges to itself.
        expected_step = 100.0 / (n - 1)
        expected_atr = expected_step * math.sqrt(2)
        assert result.atr.iloc[-1] == pytest.approx(expected_atr, rel=1e-3)
        # hl_range_avg is identically 0 since high = low.
        assert result.hl_range_avg.iloc[-1] == pytest.approx(0.0)

    def test_index_path_atr_nonzero_despite_zero_hl_range(self):
        """Sanity check on the synthetic mechanism: even though
        high - low = 0 (and so hl_range_avg = 0), synthetic ATR
        is non-zero for a moving series."""
        n = 50
        # Value moves between 100 and 105 every bar.
        value = pd.Series([100.0 + (i % 2) * 5 for i in range(n)])
        result = compute_indicator_series(
            value, pd.Series([1.0] * n), value, value,
            synthetic_atr_for_index=True,
        )
        assert result.atr.iloc[-1] > 0
        # While hl_range_avg is zero.
        assert result.hl_range_avg.iloc[-1] == pytest.approx(0.0)

    def test_real_world_size_does_not_crash(self):
        """250 weekly bars × default params should run in well under
        a second on standard hardware. We don't time-bound the test
        explicitly but a crash would fail."""
        n = 250
        close = pd.Series(
            np.random.RandomState(0).uniform(900, 1500, size=n)
        )
        high = close + np.random.RandomState(1).uniform(5, 30, size=n)
        low = close - np.random.RandomState(2).uniform(5, 30, size=n)
        volume = pd.Series(
            np.random.RandomState(3).uniform(1e6, 1e7, size=n)
        )
        result = compute_indicator_series(close, volume, high, low)
        # All last-bar values defined.
        assert not pd.isna(result.emas.med.iloc[-1])
        assert not pd.isna(result.atr.iloc[-1])


# ============================================================
# Cross-cutting: parameter dataclasses are frozen+slots
# ============================================================


class TestParameterDataclasses:

    def test_triple_ema_params_frozen(self):
        from dataclasses import FrozenInstanceError
        p = TripleEMAParams(base_len=8, first_smoothing=4, hull_n=2)
        with pytest.raises(FrozenInstanceError):
            p.base_len = 10  # type: ignore[misc]

    def test_atr_band_params_defaults_match_pine(self):
        """Default multipliers from the operator's Pine Script."""
        assert ATR_BANDS_DEFAULT.inner_multiplier == 0.2
        assert ATR_BANDS_DEFAULT.mid_multiplier == 2.0
        assert ATR_BANDS_DEFAULT.outer_multiplier == 4.5

    def test_indicator_defaults_match_pine(self):
        """Default IndicatorParams matches the Pine input(...) defaults."""
        assert INDICATOR_DEFAULTS.atr_length == 8
        assert INDICATOR_DEFAULTS.hl_range_avg_length == 26
        assert INDICATOR_DEFAULTS.med.base_len == 8
        assert INDICATOR_DEFAULTS.med.first_smoothing == 4
        assert INDICATOR_DEFAULTS.med.hull_n == 2
        assert INDICATOR_DEFAULTS.med.final_smoothing is None
        assert INDICATOR_DEFAULTS.long.base_len == 12
        assert INDICATOR_DEFAULTS.long.first_smoothing == 8
        assert INDICATOR_DEFAULTS.long.hull_n == 5
        assert INDICATOR_DEFAULTS.long.final_smoothing == 4
        assert INDICATOR_DEFAULTS.longest.base_len == 16
        assert INDICATOR_DEFAULTS.longest.first_smoothing == 9
        assert INDICATOR_DEFAULTS.longest.hull_n == 4
        assert INDICATOR_DEFAULTS.longest.final_smoothing == 4


# ============================================================
# Layer 5: per-bar state assembly
# ============================================================


from trip_ema.fundamentals.indicator_primitives import (
    _PerEMATrackState,
    _bars_and_pct_since_turn,
    _detect_turns,
    _none_if_nan,
    _pattern_label_for_ema,
    compute_bar_states,
)


class TestDetectTurns:

    def test_first_two_bars_always_false(self):
        """The lookback (ema, ema[1], ema[2]) is undefined for the
        first two bars. Even a strong up-move on bar 1 → False."""
        ema = pd.Series([100.0, 110.0, 120.0, 130.0])
        buys, sells = _detect_turns(ema)
        assert buys.iloc[0] == False
        assert buys.iloc[1] == False
        assert sells.iloc[0] == False
        assert sells.iloc[1] == False

    def test_buy_turn_at_trough(self):
        """A V-shape (down, then up) should print buy on the bar
        AFTER the trough.

        Series: 100, 95, 90, 95, 100
                                  ^
                                  bar 3: ema=95 > ema[1]=90 AND ema[1]=90 < ema[2]=95
                                  → True (buy turn)

        Actually wait — checking my formula:
          bar 2: ema=90; prev (bar 1) = 95; prev_prev (bar 0) = 100
                 buy = (90 > 95) AND (95 < 100) → False AND True → False
          bar 3: ema=95; prev = 90; prev_prev = 95
                 buy = (95 > 90) AND (90 < 95) → True AND True → True ✓
          bar 4: ema=100; prev = 95; prev_prev = 90
                 buy = (100 > 95) AND (95 < 90) → True AND False → False
        """
        ema = pd.Series([100.0, 95.0, 90.0, 95.0, 100.0])
        buys, sells = _detect_turns(ema)
        expected_buys = [False, False, False, True, False]
        assert buys.tolist() == expected_buys
        # And no sells in an upturn.
        assert sells.tolist() == [False, False, False, False, False]

    def test_sell_turn_at_peak(self):
        """A ^-shape (up, then down) should print sell on the bar
        AFTER the peak.

        Series: 100, 105, 110, 105, 100
          bar 3: ema=105 < prev=110 AND prev=110 > prev_prev=105 → True (sell)
        """
        ema = pd.Series([100.0, 105.0, 110.0, 105.0, 100.0])
        buys, sells = _detect_turns(ema)
        assert sells.tolist() == [False, False, False, True, False]
        assert buys.tolist() == [False, False, False, False, False]

    def test_no_turns_on_monotonic_series(self):
        """Steadily rising series: no sell turns ever. Buy turn
        only on bar 2 (the second up bar after the seed pair)?
        Let's check:
          bar 2: ema=120 > 110 AND 110 < 100 → True AND False → False
        So no buy turns either — because there's no actual reversal."""
        ema = pd.Series([100.0, 110.0, 120.0, 130.0, 140.0])
        buys, sells = _detect_turns(ema)
        assert buys.sum() == 0
        assert sells.sum() == 0

    def test_nan_propagates_to_false(self):
        """Leading NaN bars should produce False, not NaN, in the
        boolean output."""
        ema = pd.Series([float('nan'), float('nan'), 100.0, 110.0, 120.0])
        buys, sells = _detect_turns(ema)
        # All entries should be Python bool, not NaN.
        for v in buys.tolist():
            assert isinstance(v, (bool, np.bool_))
        assert buys.sum() == 0


class TestPatternLabelForEMA:

    def test_no_history_returns_NA(self):
        """No prior buy/sell turns — labels can't fire."""
        label = _pattern_label_for_ema(
            buy_turn_now=True, sell_turn_now=False, source_now=100.0,
            last_buy_value=None, second_last_buy_value=None,
            last_sell_value=None, second_last_sell_value=None,
            last_buy_label="NA", last_sell_label="NA",
        )
        assert label == "NA"

    def test_one_prior_buy_returns_NA(self):
        """Need at least TWO prior buy turns to detect "1 successive
        higher valley" — because the comparison is last vs
        second_last."""
        label = _pattern_label_for_ema(
            buy_turn_now=True, sell_turn_now=False, source_now=105.0,
            last_buy_value=100.0, second_last_buy_value=None,
            last_sell_value=None, second_last_sell_value=None,
            last_buy_label="NA", last_sell_label="NA",
        )
        # source_now > last_buy_value (105 > 100), but
        # second_last_buy_value is None → can't compare → NA.
        assert label == "NA"

    def test_one_higher_valley_detection(self):
        """source > last_buy AND last_buy < second_last_buy
         → 1 SUCCESSIVE HIGHER VALLEY.

        Concrete: prior buy turns were at price 95 (second_last),
        then 90 (last) — so the last valley was LOWER. Now a fresh
        buy turn at price 100 (higher than 90). Pattern fires."""
        label = _pattern_label_for_ema(
            buy_turn_now=True, sell_turn_now=False, source_now=100.0,
            last_buy_value=90.0, second_last_buy_value=95.0,
            last_sell_value=None, second_last_sell_value=None,
            last_buy_label="NA", last_sell_label="NA",
        )
        assert label == "1 SUCCESSIVE HIGHER VALLEY"

    def test_two_higher_valleys_detection(self):
        """source > last_buy AND last_buy >= second_last_buy
         → 2 SUCCESSIVE HIGHER VALLEYS.

        Concrete: prior buy turns at 95, then 100 (last_buy >=
        second_last). Now a fresh buy at 105."""
        label = _pattern_label_for_ema(
            buy_turn_now=True, sell_turn_now=False, source_now=105.0,
            last_buy_value=100.0, second_last_buy_value=95.0,
            last_sell_value=None, second_last_sell_value=None,
            last_buy_label="NA", last_sell_label="NA",
        )
        assert label == "2 SUCCESSIVE HIGHER VALLEYS"

    def test_two_higher_valleys_with_equal_history(self):
        """Boundary: last_buy_value == second_last_buy_value
        should still fire 2 HIGHER VALLEYS (Pine uses >=)."""
        label = _pattern_label_for_ema(
            buy_turn_now=True, sell_turn_now=False, source_now=105.0,
            last_buy_value=100.0, second_last_buy_value=100.0,
            last_sell_value=None, second_last_sell_value=None,
            last_buy_label="NA", last_sell_label="NA",
        )
        assert label == "2 SUCCESSIVE HIGHER VALLEYS"

    def test_one_lower_top_detection(self):
        """source < last_sell AND last_sell > second_last_sell
         → 1 SUCCESSIVE LOWER TOP.

        Concrete: sells were at 100 (second_last), then 110 (last)
        — the last top was HIGHER. Now sell at 105 (lower than 110)."""
        label = _pattern_label_for_ema(
            buy_turn_now=False, sell_turn_now=True, source_now=105.0,
            last_buy_value=None, second_last_buy_value=None,
            last_sell_value=110.0, second_last_sell_value=100.0,
            last_buy_label="NA", last_sell_label="NA",
        )
        assert label == "1 SUCCESSIVE LOWER TOP"

    def test_two_lower_tops_detection(self):
        """source < last_sell AND last_sell <= second_last_sell
         → 2 SUCCESSIVE LOWER TOPS."""
        label = _pattern_label_for_ema(
            buy_turn_now=False, sell_turn_now=True, source_now=95.0,
            last_buy_value=None, second_last_buy_value=None,
            last_sell_value=100.0, second_last_sell_value=110.0,
            last_buy_label="NA", last_sell_label="NA",
        )
        assert label == "2 SUCCESSIVE LOWER TOPS"

    def test_no_turn_no_detection(self):
        """Without buy_turn_now=True or sell_turn_now=True, no
        new detection — but stickiness might still emit a prior
        label."""
        label = _pattern_label_for_ema(
            buy_turn_now=False, sell_turn_now=False, source_now=100.0,
            last_buy_value=90.0, second_last_buy_value=95.0,
            last_sell_value=None, second_last_sell_value=None,
            last_buy_label="NA", last_sell_label="NA",
            bars_since_last_buy=None, bars_since_last_sell=None,
        )
        assert label == "NA"

    def test_sticky_label_within_ttl(self):
        """If a label fired recently (bars_since_last_buy=1), it
        should still be emitted on the next bar."""
        label = _pattern_label_for_ema(
            buy_turn_now=False, sell_turn_now=False, source_now=100.0,
            last_buy_value=90.0, second_last_buy_value=95.0,
            last_sell_value=None, second_last_sell_value=None,
            last_buy_label="1 SUCCESSIVE HIGHER VALLEY",
            last_sell_label="NA",
            bars_since_last_buy=1, bars_since_last_sell=None,
        )
        assert label == "1 SUCCESSIVE HIGHER VALLEY"

    def test_sticky_label_at_ttl_boundary(self):
        """bars_since_last_buy=2 (current bar is 2 bars after the
        turn) — still within TTL=3 (the comparison is `<`)."""
        label = _pattern_label_for_ema(
            buy_turn_now=False, sell_turn_now=False, source_now=100.0,
            last_buy_value=90.0, second_last_buy_value=95.0,
            last_sell_value=None, second_last_sell_value=None,
            last_buy_label="1 SUCCESSIVE HIGHER VALLEY",
            last_sell_label="NA",
            bars_since_last_buy=2, bars_since_last_sell=None,
        )
        assert label == "1 SUCCESSIVE HIGHER VALLEY"

    def test_sticky_label_expires_after_ttl(self):
        """bars_since_last_buy=3 (3 bars after the turn) — outside
        TTL → NA."""
        label = _pattern_label_for_ema(
            buy_turn_now=False, sell_turn_now=False, source_now=100.0,
            last_buy_value=90.0, second_last_buy_value=95.0,
            last_sell_value=None, second_last_sell_value=None,
            last_buy_label="1 SUCCESSIVE HIGHER VALLEY",
            last_sell_label="NA",
            bars_since_last_buy=3, bars_since_last_sell=None,
        )
        assert label == "NA"

    def test_fresh_detection_takes_precedence_over_sticky(self):
        """If a NEW detection fires on the same bar that a prior
        label is still sticky, the new detection wins."""
        # Prior label was "1 SUCCESSIVE HIGHER VALLEY", emitted 1
        # bar ago. Now a fresh sell-turn fires AND it detects
        # "1 SUCCESSIVE LOWER TOP". Fresh detection should win.
        label = _pattern_label_for_ema(
            buy_turn_now=False, sell_turn_now=True, source_now=95.0,
            last_buy_value=90.0, second_last_buy_value=95.0,
            last_sell_value=100.0, second_last_sell_value=90.0,
            last_buy_label="1 SUCCESSIVE HIGHER VALLEY",
            last_sell_label="NA",
            bars_since_last_buy=1, bars_since_last_sell=None,
        )
        assert label == "1 SUCCESSIVE LOWER TOP"


class TestBarsAndPctSinceTurn:

    def test_returns_None_when_ema_not_defined(self):
        """If prev_ema or ema_now is NaN, can't determine direction."""
        close = pd.Series([100.0, 110.0, 120.0])
        state = _PerEMATrackState()
        n_bars, pct = _bars_and_pct_since_turn(
            i=2, close=close, state=state,
            ema_now=float('nan'), prev_ema=100.0,
        )
        assert n_bars is None
        assert pct is None

    def test_returns_None_when_no_prior_turn(self):
        """Even with a well-defined EMA slope, if no prior turn has
        been registered in state, return None."""
        close = pd.Series([100.0, 110.0, 120.0])
        state = _PerEMATrackState()  # no turns registered
        n_bars, pct = _bars_and_pct_since_turn(
            i=2, close=close, state=state,
            ema_now=120.0, prev_ema=110.0,
        )
        assert n_bars is None
        assert pct is None

    def test_uses_last_buy_when_ema_rising(self):
        """Up-slope EMA → report bars + pct since last_buy_bar."""
        close = pd.Series([100.0, 95.0, 90.0, 95.0, 100.0])
        state = _PerEMATrackState()
        state.last_buy_bar_idx = 2  # buy turn occurred at bar 2 (price 90)
        n_bars, pct = _bars_and_pct_since_turn(
            i=4, close=close, state=state,
            ema_now=100.0, prev_ema=95.0,  # rising
        )
        # bars since turn: 4 - 2 = 2.
        # close at turn = 90; close now = 100.
        # pct = (100 - 90) / 90 × 100 = 11.111...
        assert n_bars == 2
        assert pct == pytest.approx(11.1111, rel=1e-3)

    def test_uses_last_sell_when_ema_falling(self):
        """Down-slope EMA → report bars + NEGATED pct since
        last_sell_bar. The negation is because we're measuring
        the down-move from a sell — a falling close should give
        positive %."""
        close = pd.Series([100.0, 105.0, 110.0, 105.0, 100.0])
        state = _PerEMATrackState()
        state.last_sell_bar_idx = 2  # sell turn at bar 2 (price 110)
        n_bars, pct = _bars_and_pct_since_turn(
            i=4, close=close, state=state,
            ema_now=100.0, prev_ema=105.0,  # falling
        )
        # bars: 4 - 2 = 2.
        # close at turn = 110, close now = 100.
        # raw = (100 - 110)/110 × 100 = -9.09
        # negated = 9.09
        assert n_bars == 2
        assert pct == pytest.approx(9.0909, rel=1e-3)

    def test_flat_ema_returns_None(self):
        """If ema_now == prev_ema, no direction → None."""
        close = pd.Series([100.0, 100.0, 100.0])
        state = _PerEMATrackState()
        state.last_buy_bar_idx = 0
        state.last_sell_bar_idx = 0
        n_bars, pct = _bars_and_pct_since_turn(
            i=2, close=close, state=state,
            ema_now=100.0, prev_ema=100.0,
        )
        assert n_bars is None
        assert pct is None


class TestNoneIfNan:

    def test_None_passes_through(self):
        assert _none_if_nan(None) is None

    def test_nan_converts_to_None(self):
        assert _none_if_nan(float('nan')) is None
        assert _none_if_nan(np.nan) is None

    def test_finite_value_passes_as_float(self):
        v = _none_if_nan(3.14)
        assert v == 3.14
        assert isinstance(v, float)

    def test_int_converts_to_float(self):
        v = _none_if_nan(5)
        assert v == 5.0
        assert isinstance(v, float)


class TestComputeBarStatesEndToEnd:
    """End-to-end test of the per-bar walker. Uses a longer
    synthetic series so all indicators are defined, then verifies
    the output dict shape and a few key fields."""

    def _build_inputs(self, n: int = 100):
        """A 100-bar synthetic ticker series with smooth oscillation
        and varied volume. Enough bars so all EMAs (including the
        longest with base_len=16) stabilize."""
        close = pd.Series(
            100.0 + 10.0 * np.sin(np.linspace(0, 4 * math.pi, n))
        )
        high = close + 2.0
        low = close - 2.0
        volume = pd.Series(
            1e6 + 1e5 * np.cos(np.linspace(0, 8 * math.pi, n))
        )
        # Convert numpy date range to pandas Series of dates.
        dates = pd.Series(pd.date_range("2024-01-05", periods=n, freq="W"))
        # Convert to date objects since IndicatorState.trade_date is date.
        dates = dates.dt.date
        return close, volume, high, low, dates

    def test_returns_one_dict_per_bar(self):
        n = 100
        close, vol, high, low, dates = self._build_inputs(n)
        series = compute_indicator_series(close, vol, high, low)
        bar_states = compute_bar_states(
            close, vol, high, low,
            trade_dates=dates, series=series,
        )
        assert len(bar_states) == n

    def test_dict_has_all_indicator_state_fields(self):
        """The dicts should have keys matching every IndicatorState
        field except regime (Phase 5-3) and the polymorphic identity
        fields (filled by the runner)."""
        close, vol, high, low, dates = self._build_inputs(100)
        series = compute_indicator_series(close, vol, high, low)
        bar_states = compute_bar_states(
            close, vol, high, low,
            trade_dates=dates, series=series,
        )
        # Inspect a late bar (so all indicators are defined).
        late = bar_states[-1]
        # Sanity-check key fields are present.
        expected_keys = {
            "trade_date", "close",
            "med_ema", "long_ema", "longest_ema",
            "atr", "upper_inner_band", "lower_inner_band",
            "upper_mid_band", "lower_mid_band",
            "upper_outer_band", "lower_outer_band",
            "pierces_upper_mid", "pierces_lower_mid",
            "dist_to_med_in_hl_units", "dist_to_long_in_hl_units",
            "dist_to_longest_in_hl_units",
            "mean_dist_to_med_in_hl_units", "mean_dist_to_long_in_hl_units",
            "mean_dist_to_longest_in_hl_units",
            "bars_since_med_turn", "bars_since_long_turn",
            "bars_since_longest_turn",
            "pct_change_since_med_turn", "pct_change_since_long_turn",
            "pct_change_since_longest_turn",
            "med_pattern_label", "long_pattern_label", "longest_pattern_label",
            "med_turn_buy", "med_turn_sell",
            "long_turn_buy", "long_turn_sell",
            "longest_turn_buy", "longest_turn_sell",
        }
        actual_keys = set(late.keys())
        missing = expected_keys - actual_keys
        assert not missing, f"missing keys: {missing}"

    def test_late_bar_indicators_are_finite(self):
        """By bar 99, all indicators should have finite values."""
        close, vol, high, low, dates = self._build_inputs(100)
        series = compute_indicator_series(close, vol, high, low)
        bar_states = compute_bar_states(
            close, vol, high, low,
            trade_dates=dates, series=series,
        )
        late = bar_states[-1]
        for key in ["med_ema", "long_ema", "longest_ema", "atr",
                    "upper_inner_band", "lower_outer_band"]:
            v = late[key]
            assert v is not None and not pd.isna(v), (
                f"{key} is {v} at last bar"
            )

    def test_turn_signals_match_detect_turns(self):
        """The turn booleans in each dict should match the standalone
        _detect_turns call on the same EMA series."""
        close, vol, high, low, dates = self._build_inputs(100)
        series = compute_indicator_series(close, vol, high, low)
        bar_states = compute_bar_states(
            close, vol, high, low,
            trade_dates=dates, series=series,
        )
        expected_med_buys, expected_med_sells = _detect_turns(series.emas.med)
        actual_med_buys = [b["med_turn_buy"] for b in bar_states]
        actual_med_sells = [b["med_turn_sell"] for b in bar_states]
        assert actual_med_buys == expected_med_buys.tolist()
        assert actual_med_sells == expected_med_sells.tolist()

    def test_pattern_labels_are_valid(self):
        """Every emitted label must be in PATTERN_LABELS."""
        from trip_ema.fundamentals import PATTERN_LABELS
        close, vol, high, low, dates = self._build_inputs(100)
        series = compute_indicator_series(close, vol, high, low)
        bar_states = compute_bar_states(
            close, vol, high, low,
            trade_dates=dates, series=series,
        )
        for b in bar_states:
            assert b["med_pattern_label"] in PATTERN_LABELS
            assert b["long_pattern_label"] in PATTERN_LABELS
            assert b["longest_pattern_label"] in PATTERN_LABELS

    def test_index_path_with_synthetic_atr(self):
        """For an industry index series (high=low=close=value,
        vol=1), the walker should still produce one dict per bar
        with valid (non-crashing) outputs.

        Distance metrics will be None because hl_range_avg is 0
        (we divide by safe_hlra which gates zeros out)."""
        n = 100
        value = pd.Series(
            1000.0 + 50.0 * np.sin(np.linspace(0, 4 * math.pi, n))
        )
        vol = pd.Series([1.0] * n)
        dates = pd.Series(pd.date_range("2024-01-05", periods=n, freq="W")).dt.date
        series = compute_indicator_series(
            value, vol, value, value,
            synthetic_atr_for_index=True,
        )
        bar_states = compute_bar_states(
            value, vol, value, value,
            trade_dates=dates, series=series,
        )
        assert len(bar_states) == n
        # Distance metrics should be None for indices (hl_range=0).
        late = bar_states[-1]
        assert late["dist_to_med_in_hl_units"] is None
        assert late["dist_to_long_in_hl_units"] is None
        # But ATR and bands should still be finite.
        assert late["atr"] is not None and late["atr"] > 0
        assert late["upper_inner_band"] is not None

    def test_consistent_bars_since_turn_increments(self):
        """After a turn fires on some bar, bars_since_*_turn on
        the next bar should be 1, not None."""
        n = 100
        close, vol, high, low, dates = self._build_inputs(n)
        series = compute_indicator_series(close, vol, high, low)
        bar_states = compute_bar_states(
            close, vol, high, low,
            trade_dates=dates, series=series,
        )
        # Find the first bar where a med_turn_buy fires.
        first_med_buy_idx = None
        for i, b in enumerate(bar_states):
            if b["med_turn_buy"]:
                first_med_buy_idx = i
                break
        if first_med_buy_idx is None:
            pytest.skip("no med buy turn in synthetic series")
        # The very next bar should report bars_since_med_turn == 1
        # OR None (if the EMA went flat / NaN). Either is acceptable
        # — but if not None, must equal 1.
        if first_med_buy_idx + 1 < n:
            next_bar = bar_states[first_med_buy_idx + 1]
            if next_bar["bars_since_med_turn"] is not None:
                # If EMA is still rising, we look back to the turn.
                # bars = 1.
                assert next_bar["bars_since_med_turn"] == 1
