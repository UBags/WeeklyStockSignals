"""C2 — the five band-ruler ATRs, and the branch that must carry them.

Two things are being pinned here.

1. ARITHMETIC. Wilder RMA of true range, seeded on the first TR. Checked
   against an independent reference loop rather than against a golden number
   copied out of the implementation, because a golden number copied out of
   the implementation proves only that the implementation has not changed.

2. THE BRANCH. The signal walk snapshots the assembler at the start of each
   week, replays the forming bar for each signal day, then restores. If the
   new ATR state is not in the snapshot tuple, that restore silently leaves
   the ATRs holding the forming branch's values — every subsequent week's
   bands would be computed against an ATR that saw a bar twice. That failure
   is invisible in the output: no exception, no NaN, just slightly wrong
   numbers in 100k rows. Hence the round-trip test.
"""
from __future__ import annotations

import math

import pytest

from trip_ema.fundamentals.signal_bands import ATR_PERIODS
from trip_ema.fundamentals.structure_series import StructureSeriesEngine
from trip_ema.fundamentals.signal_features import SignalFeatureAssembler

# (close, high, low) — includes a gap up (bar 3) and a gap down (bar 6) so
# the |high - prev_close| and |low - prev_close| legs of TR actually fire.
BARS = [
    (100.0, 101.0, 99.0),
    (102.0, 103.0, 100.5),
    (101.0, 102.5, 100.0),
    (110.0, 111.0, 108.0),      # gap up: low 108 > prev close 101
    (109.0, 112.0, 108.5),
    (111.0, 111.5, 108.0),
    (95.0, 97.0, 94.0),         # gap down: high 97 < prev close 111
    (96.0, 98.0, 95.0),
    (99.0, 99.5, 95.5),
    (98.0, 100.0, 97.5),
    (103.0, 104.0, 97.0),
    (102.0, 103.5, 101.0),
]


def _ref_atr(bars, n):
    """Independent Wilder RMA of TR: seed on the first TR, then a = 1/n.
    Returns the value after each bar."""
    out, prev_close, atr = [], None, None
    for close, high, low in bars:
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        atr = tr if atr is None else (1.0 / n) * tr + (1.0 - 1.0 / n) * atr
        out.append(atr)
        prev_close = close
    return out


def _feed(eng, bars):
    """Advance the engine over bars; return the per-bar output dicts. The
    EMAs are handed in as constants — this file is about the ATRs, and the
    §4 series have their own test module."""
    outs = []
    for close, high, low in bars:
        outs.append(eng.update(
            close=close, high=high, low=low,
            med_ema=100.0, long_ema=98.0, longest_ema=95.0, atr=2.0,
        ))
    return outs


class TestArithmetic:

    @pytest.mark.parametrize("n", ATR_PERIODS)
    def test_matches_an_independent_wilder_rma(self, n):
        outs = _feed(StructureSeriesEngine(), BARS)
        expected = _ref_atr(BARS, n)
        for i, (o, e) in enumerate(zip(outs, expected, strict=True)):
            assert o[f"atr{n}"] == pytest.approx(e), f"bar {i}, atr{n}"

    def test_first_bar_seeds_every_period_on_the_same_true_range(self):
        out = _feed(StructureSeriesEngine(), BARS[:1])[0]
        tr0 = BARS[0][1] - BARS[0][2]          # high - low, no prev close
        for n in ATR_PERIODS:
            assert out[f"atr{n}"] == pytest.approx(tr0)

    def test_true_range_uses_the_previous_close_on_a_gap(self):
        """Bar 3 gaps up: high-low is 3.0 but |high - prev_close| is 10.0.
        A TR that ignored the previous close would understate it by 7."""
        outs = _feed(StructureSeriesEngine(), BARS[:4])
        atr10_bar3 = outs[3]["atr10"]
        prev = outs[2]["atr10"]
        tr3 = max(111.0 - 108.0, abs(111.0 - 101.0), abs(108.0 - 101.0))
        assert tr3 == pytest.approx(10.0)
        assert atr10_bar3 == pytest.approx(0.1 * tr3 + 0.9 * prev)

    def test_tighter_period_reacts_faster(self):
        """After a volatility shock, the short ATR must be above the long
        one. If this ever flips, the alpha is inverted somewhere."""
        outs = _feed(StructureSeriesEngine(), BARS)
        last = outs[-1]
        assert last["atr10"] > last["atr20"] > last["atr40"]

    def test_atr28_is_unchanged_by_this_commit(self):
        outs = _feed(StructureSeriesEngine(), BARS)
        expected = _ref_atr(BARS, 28)
        for o, e in zip(outs, expected, strict=True):
            assert o["atr28"] == pytest.approx(e)


class TestOutputShape:

    def test_every_period_is_emitted(self):
        out = _feed(StructureSeriesEngine(), BARS)[-1]
        for n in ATR_PERIODS:
            assert f"atr{n}" in out
        assert "atr28" in out              # the normaliser stays

    def test_no_stray_periods(self):
        out = _feed(StructureSeriesEngine(), BARS)[-1]
        atr_keys = {k for k in out if k.startswith("atr")}
        assert atr_keys == {"atr28"} | {f"atr{n}" for n in ATR_PERIODS}


class TestSnapshotRestore:
    """The walk's branch, in miniature."""

    def test_restore_rewinds_the_atrs(self):
        eng = StructureSeriesEngine()
        _feed(eng, BARS[:6])
        snap = eng.snapshot()

        _feed(eng, [(200.0, 250.0, 190.0)])   # a violent forming bar
        branched = _feed(eng, BARS[6:7])[0]

        eng.restore(snap)                     # discard the branch
        after = _feed(eng, BARS[6:7])[0]

        clean = _feed(StructureSeriesEngine(), BARS[:7])[-1]
        for n in ATR_PERIODS:
            assert after[f"atr{n}"] == pytest.approx(clean[f"atr{n}"]), (
                f"atr{n} did not rewind — the snapshot tuple is missing it"
            )
            assert branched[f"atr{n}"] != pytest.approx(clean[f"atr{n}"]), (
                f"atr{n} test is vacuous: the branch did not move it"
            )

    def test_snapshot_is_hashable_and_flat(self):
        """Tuples all the way down — the walk holds these across weeks and a
        mutable member would alias the live state."""
        eng = StructureSeriesEngine()
        _feed(eng, BARS[:3])
        snap = eng.snapshot()
        hash(snap)                            # raises if anything is a dict

    def test_restore_of_a_wrong_width_snapshot_raises(self):
        eng = StructureSeriesEngine()
        _feed(eng, BARS[:3])
        snap = eng.snapshot()
        with pytest.raises(ValueError):
            eng.restore(snap[:-1] + ((None,) * (len(ATR_PERIODS) - 1),))


class TestAssemblerAccessor:

    def test_atrs_before_any_bar_is_all_nan(self):
        asm = SignalFeatureAssembler()
        atrs = asm.atrs()
        assert set(atrs) == set(ATR_PERIODS)
        assert all(math.isnan(v) for v in atrs.values())

    def test_atrs_tracks_the_engine(self):
        asm = SignalFeatureAssembler()
        for close, high, low in BARS:
            asm.update(
                close=close, high=high, low=low,
                med_ema=100.0, long_ema=98.0, longest_ema=95.0, atr=2.0,
            )
        atrs = asm.atrs()
        for n in ATR_PERIODS:
            assert atrs[n] == pytest.approx(_ref_atr(BARS, n)[-1])

    def test_the_new_atrs_stay_out_of_the_features_jsonb(self):
        """They are columns now. A number in a column AND in the jsonb is a
        number that will disagree with itself the first time one of the two
        write paths is edited."""
        asm = SignalFeatureAssembler()
        for close, high, low in BARS:
            asm.update(
                close=close, high=high, low=low,
                med_ema=100.0, long_ema=98.0, longest_ema=95.0, atr=2.0,
            )
        feats = asm.features()
        for n in ATR_PERIODS:
            assert f"atr{n}" not in feats
        assert "atr28" in feats              # the normaliser is a feature
