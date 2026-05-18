"""Tests for VolumeWeightedTripleEMA.

The Pine reference values aren't available yet (per the user's plan to
add them later), so these tests verify:
  1. Mathematical sanity (constant input → constant output after warmup).
  2. Recurrence equivalence: feeding bars one at a time matches the
     batch `compute_series` helper bar-for-bar.
  3. Warmup behavior (NaN before sufficient samples).
  4. Edge cases: zero volume, parameter validation.
"""

from __future__ import annotations

import math

import pytest

from trip_ema.indicators.vw_ema import (
    VolumeWeightedTripleEMA,
    compute_series,
    _wma,
)


# -------------------------- _wma helper -------------------------- #

def test_wma_simple_three_values():
    # values [10, 20, 30] with length 3: weights 1, 2, 3 → sum = 6
    # weighted = 1*10 + 2*20 + 3*30 = 10 + 40 + 90 = 140
    # result = 140 / 6
    assert _wma([10.0, 20.0, 30.0], 3) == pytest.approx(140.0 / 6.0)


def test_wma_uses_only_last_n_values():
    # length-2 over [10, 20, 30] should use only [20, 30]
    # weights 1, 2: weighted = 20 + 60 = 80; sum = 3 → 80/3
    assert _wma([10.0, 20.0, 30.0], 2) == pytest.approx(80.0 / 3.0)


def test_wma_returns_nan_when_insufficient_data():
    assert math.isnan(_wma([10.0, 20.0], 3))


# -------------------------- Construction validation -------------------------- #

def test_lengths_below_two_rejected():
    with pytest.raises(ValueError):
        VolumeWeightedTripleEMA(base=1, s1=4, s2=4)
    with pytest.raises(ValueError):
        VolumeWeightedTripleEMA(base=4, s1=1, s2=4)
    with pytest.raises(ValueError):
        VolumeWeightedTripleEMA(base=4, s1=4, s2=1)


def test_final_below_two_rejected():
    with pytest.raises(ValueError):
        VolumeWeightedTripleEMA(base=4, s1=4, s2=4, final=1)


def test_final_none_is_allowed():
    # Should not raise; matches Pine's `med` configuration.
    VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None)


# -------------------------- Mathematical sanity -------------------------- #

def test_constant_input_converges_to_constant():
    """With constant close and volume, the EMA must converge to the constant."""
    ema = VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None)
    last = float("nan")
    for _ in range(200):
        last = ema.update(close=100.0, volume=1000.0)
    assert not math.isnan(last)
    assert last == pytest.approx(100.0, rel=1e-9)


def test_constant_input_with_final_stage_also_converges():
    ema = VolumeWeightedTripleEMA(base=15, s1=4, s2=4, final=3)
    last = float("nan")
    for _ in range(200):
        last = ema.update(close=50.0, volume=2000.0)
    assert last == pytest.approx(50.0, rel=1e-9)


# -------------------------- Warmup -------------------------- #

def test_warmup_produces_nan_until_enough_bars():
    """The WMA stages require ~s2 + sqrt(s2) - 1 vw2 values before producing
    a real output. Before that, update() must return NaN."""
    s2 = 4
    ema = VolumeWeightedTripleEMA(base=8, s1=4, s2=s2, final=None)
    # We expect NaN for the first (s2 + floor(sqrt(s2)) - 1 - 1) bars,
    # then a real number. With s2=4, sqrt=2, so we need 5 vw2 values for
    # the first real output.
    outputs = []
    for i in range(10):
        outputs.append(ema.update(close=100.0 + i, volume=1000.0))
    # First few should be NaN
    assert math.isnan(outputs[0])
    # Last few should be real
    assert not math.isnan(outputs[-1])


# -------------------------- Recurrence equivalence -------------------------- #

def test_incremental_matches_batch():
    """compute_series and a fresh per-bar feed must produce identical output."""
    closes = [100.0 + i * 0.5 for i in range(50)]
    volumes = [1000.0 + i * 10.0 for i in range(50)]

    batch = compute_series(closes, volumes, base=8, s1=4, s2=4, final=None)

    incremental_ema = VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None)
    incremental = [incremental_ema.update(c, v) for c, v in zip(closes, volumes)]

    for i, (b, inc) in enumerate(zip(batch, incremental)):
        if math.isnan(b) and math.isnan(inc):
            continue
        assert b == pytest.approx(inc, rel=1e-12), f"mismatch at bar {i}: batch={b} inc={inc}"


def test_incremental_matches_batch_with_final_stage():
    closes = [50.0 + math.sin(i / 5.0) * 5 for i in range(80)]
    volumes = [500.0 + math.cos(i / 7.0) * 50 for i in range(80)]

    batch = compute_series(closes, volumes, base=15, s1=4, s2=4, final=3)
    inc_ema = VolumeWeightedTripleEMA(base=15, s1=4, s2=4, final=3)
    incremental = [inc_ema.update(c, v) for c, v in zip(closes, volumes)]

    for i, (b, inc) in enumerate(zip(batch, incremental)):
        if math.isnan(b) and math.isnan(inc):
            continue
        assert b == pytest.approx(inc, rel=1e-12)


# -------------------------- Edge cases -------------------------- #

def test_zero_volume_does_not_crash():
    """A zero-volume bar makes the VW denominator zero. We return NaN rather
    than raising or producing infinity."""
    ema = VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None)
    result = ema.update(close=100.0, volume=0.0)
    assert math.isnan(result)


def test_compute_series_length_mismatch_raises():
    with pytest.raises(ValueError):
        compute_series([1.0, 2.0], [1.0], base=4, s1=4, s2=4, final=None)


def test_output_responds_to_input_changes():
    """After warmup, a step change in input must cause the output to move
    in the same direction. Basic monotonicity sanity."""
    ema = VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None)
    # Warm up at 100
    for _ in range(100):
        ema.update(close=100.0, volume=1000.0)
    converged = ema.update(close=100.0, volume=1000.0)
    # Now step up
    next_val = ema.update(close=110.0, volume=1000.0)
    assert next_val > converged


# ============================================================
# B1: Volume SMA smoothing (Pine fidelity)
# ============================================================


class TestVolumeSmoothing:
    """The Pine Weekly_Triple_EMA wraps raw volume in `ta.sma(volume, 2)`
    before using it in the VW-EMA. This was missing in earlier Python
    versions and caused divergence from TradingView."""

    def test_default_volume_sma_length_is_1(self):
        """Backwards compat: default is no smoothing (matches the
        pre-B1 behavior). Match-Pine clients pass volume_sma_length=2."""
        ema = VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None)
        assert ema.volume_sma_length == 1

    def test_smoothing_length_below_1_rejected(self):
        with pytest.raises(ValueError):
            VolumeWeightedTripleEMA(
                base=8, s1=4, s2=4, final=None, volume_sma_length=0,
            )

    def test_smoothing_length_1_equals_raw_volume(self):
        """With length=1, the SMA buffer holds only the most recent
        volume, so the smoothed value equals the raw volume itself."""
        ema_raw = VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None,
                                            volume_sma_length=1)
        for i, (c, v) in enumerate([(100, 1000), (101, 1100), (102, 1200),
                                      (103, 900), (104, 1500)]):
            ema_raw.update(c, v)
        # The buffer should hold only the last volume
        assert list(ema_raw._volume_window) == [1500]

    def test_smoothing_length_2_holds_two_volumes(self):
        ema = VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None,
                                        volume_sma_length=2)
        ema.update(100, 1000)
        assert list(ema._volume_window) == [1000]
        ema.update(101, 2000)
        assert list(ema._volume_window) == [1000, 2000]
        ema.update(102, 3000)
        # Oldest evicted
        assert list(ema._volume_window) == [2000, 3000]

    def test_smoothing_changes_output(self):
        """With deliberately-spikey volume, smoothed and raw should
        produce different outputs."""
        ema_raw = VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None,
                                            volume_sma_length=1)
        ema_smooth = VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None,
                                               volume_sma_length=2)

        # 60 bars, normal volume, then one massive spike at bar 50
        for i in range(60):
            close = 100 + i * 0.5
            volume = 1000.0 if i != 50 else 100000.0
            ema_raw.update(close, volume)
            ema_smooth.update(close, volume)

        # The spike should affect the raw output more than the smoothed
        # one — at least one of the outputs after the spike should
        # differ measurably.
        raw_val = ema_raw._vw1_num / ema_raw._vw1_den
        sm_val = ema_smooth._vw1_num / ema_smooth._vw1_den
        # They will differ — exact magnitudes depend on the EMA decay,
        # but at length=2 smoothing the spike is averaged with the prior
        # bar's volume, which substantially dampens it.
        assert raw_val != pytest.approx(sm_val)

    def test_snapshot_restore_preserves_volume_window(self):
        """The snapshot/rollback mechanism must round-trip the volume
        buffer too, or A1's invariants would break with smoothing on."""
        ema = VolumeWeightedTripleEMA(base=8, s1=4, s2=4, final=None,
                                        volume_sma_length=3)
        for c, v in [(100, 1000), (101, 1100), (102, 1200), (103, 1300)]:
            ema.update(c, v)

        snap = ema._snapshot()
        before = list(ema._volume_window)

        # Mutate state
        ema.update(104, 5000)
        ema.update(105, 6000)
        assert list(ema._volume_window) != before

        # Restore
        ema._restore(snap)
        assert list(ema._volume_window) == before


# ============================================================
# Phase 9 — B3 regression: Pine NaN warmup fidelity
#
# Pine's `ta.sma(volume, N)` returns NaN until N samples are available.
# The Python port previously seeded the EMA from bar 1 even when
# `volume_sma_length=2`, which produced a 1-bar phase shift vs. Pine.
# The fix returns NaN until the volume window is fully populated.
# ============================================================


class TestPineNaNWarmupFidelity:

    def test_bar_1_returns_nan_when_volume_sma_length_2(self):
        """B3: with Pine's volume_sma_length=2, the first bar must
        return NaN because ta.sma(volume, 2) needs two samples."""
        ema = VolumeWeightedTripleEMA(
            base=8, s1=4, s2=4, final=None, volume_sma_length=2,
        )
        result_bar_1 = ema.update(100.0, 1000.0)
        assert math.isnan(result_bar_1), (
            f"Bar 1 with volume_sma_length=2 must return NaN to match Pine; "
            f"got {result_bar_1}"
        )

    def test_bar_1_returns_normally_when_volume_sma_length_1(self):
        """With volume_sma_length=1 (no smoothing), bar 1 should NOT
        return NaN due to volume — only the EMA's own warmup applies.
        At bar 1, the EMA stages haven't warmed up yet either, so we
        still see NaN, but for a different reason. We assert specifically
        that the volume-NaN-gate isn't tripped (by checking with a long
        enough warmup that we'd see real values)."""
        ema = VolumeWeightedTripleEMA(
            base=8, s1=4, s2=4, final=None, volume_sma_length=1,
        )
        # Bar 1 — EMA hasn't warmed up; volume gate is NOT the cause
        ema.update(100.0, 1000.0)
        # The internal _vw1_num must have been seeded (proves we didn't
        # bail out due to the volume gate). With smoothing length 1,
        # smoothed_volume == volume == 1000, so cv = 100 * 1000 = 100000.
        assert ema._vw1_num == 100_000.0
        assert ema._vw1_den == 1000.0

    def test_bar_1_does_not_seed_vw1_when_sma_length_2(self):
        """B3 corollary: when bar 1 returns NaN due to the volume gate,
        the VW-EMA recurrence's internal state (_vw1_num, _vw1_den) must
        NOT have been advanced. The whole point of the early return is
        to keep the EMA waiting for the smoothed-volume's first valid
        sample."""
        ema = VolumeWeightedTripleEMA(
            base=8, s1=4, s2=4, final=None, volume_sma_length=2,
        )
        ema.update(100.0, 1000.0)
        # Internal state untouched
        assert ema._vw1_num is None
        assert ema._vw1_den is None
        assert ema._vw2 is None
        assert len(ema._vw2_window) == 0
        # But the volume window itself was updated
        assert list(ema._volume_window) == [1000.0]

    def test_bar_2_initializes_vw1_with_smoothed_volume(self):
        """Bar 2 has 2 samples in the window → SMA = avg → VW-EMA gets
        its first real seed. The seed must use the smoothed volume,
        not raw volume."""
        ema = VolumeWeightedTripleEMA(
            base=8, s1=4, s2=4, final=None, volume_sma_length=2,
        )
        ema.update(100.0, 1000.0)   # NaN, no seed
        ema.update(101.0, 2000.0)   # bar 2 — SMA = 1500, seed happens here
        # Seed: cv = 101 * 1500 = 151500, den = 1500
        assert ema._vw1_num == pytest.approx(151_500.0)
        assert ema._vw1_den == pytest.approx(1500.0)

    def test_no_phase_shift_vs_pine_after_warmup(self):
        """Sanity: a long constant input should still converge to the
        constant — the NaN-warmup change shouldn't break steady state.
        Just confirms the change didn't permanently break the math."""
        ema = VolumeWeightedTripleEMA(
            base=8, s1=4, s2=4, final=None, volume_sma_length=2,
        )
        results = [ema.update(100.0, 1000.0) for _ in range(80)]
        # Past warmup, the value must converge to 100
        assert results[-1] == pytest.approx(100.0, abs=0.01)

