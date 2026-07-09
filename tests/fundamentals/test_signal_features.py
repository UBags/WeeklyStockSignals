"""Tests for the signal feature assembler (spec v0.3 §6-§10)."""
import math

import numpy as np
import pandas as pd

from trip_ema.fundamentals.pattern_rules import (
    REL_HH, REL_HL, REL_LH, REL_LL, REL_NA,
)
from trip_ema.fundamentals.signal_features import (
    DIV_HID_BEAR, DIV_HID_BULL, DIV_NONE, DIV_REG_BEAR, DIV_REG_BULL,
    SignalFeatureAssembler, _MEMBERS, _divergence,
)


class TestDivergence:
    def test_regular_bear(self):
        assert _divergence(REL_HH, REL_NA, REL_LH, REL_NA) == DIV_REG_BEAR

    def test_regular_bull(self):
        assert _divergence(REL_NA, REL_LL, REL_NA, REL_HL) == DIV_REG_BULL

    def test_hidden_bear(self):
        assert _divergence(REL_LH, REL_NA, REL_HH, REL_NA) == DIV_HID_BEAR

    def test_hidden_bull(self):
        assert _divergence(REL_NA, REL_HL, REL_NA, REL_LL) == DIV_HID_BULL

    def test_agreement_is_none(self):
        assert _divergence(REL_HH, REL_HL, REL_HH, REL_HL) == DIV_NONE


def _fixture(n=300, seed=3):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    sp = np.abs(rng.normal(0, 1, n)) + 0.3
    high, low = close + sp, close - sp
    c = pd.Series(close)
    med = c.ewm(span=8, adjust=False).mean().to_numpy()
    lng = c.ewm(span=21, adjust=False).mean().to_numpy()
    lst = c.ewm(span=50, adjust=False).mean().to_numpy()
    atr = pd.Series(high - low).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    return close, high, low, med, lng, lst, atr


class TestAssembly:
    def test_feature_dict_complete_and_valid(self):
        close, high, low, med, lng, lst, atr = _fixture()
        asm = SignalFeatureAssembler()
        for i in range(len(close)):
            asm.update(close=float(close[i]), high=float(high[i]),
                       low=float(low[i]), med_ema=float(med[i]),
                       long_ema=float(lng[i]), longest_ema=float(lst[i]),
                       atr=float(atr[i]))
        f = asm.features()
        exp = set()
        for name, _s, _k in _MEMBERS:
            exp |= {f"{name}_structure", f"bars_since_{name}_turn",
                    f"pdisp_{name}"}
        exp |= {f"div_{o}" for o in ("srsi14", "smacd", "smoothed_RSI_diff")}
        exp |= {"srsi14", "srsi28", "smoothed_RSI_diff",
                "ema_smoothed_RSI_diff", "smacd", "macd_hist", "sdosc",
                "ema_diff_1", "ema_diff_2", "ema_diff_3", "ema_ema_diff_1",
                "ema_ema_diff_2", "ema_ema_diff_3", "atr28"}
        assert set(f) == exp
        valid = {"NA", "UPTREND", "DOWNTREND", "BROADENING", "CONTRACTING"}
        assert all(f[f"{n}_structure"] in valid for n, _s, _k in _MEMBERS)
        divs = {DIV_NONE, DIV_REG_BEAR, DIV_REG_BULL, DIV_HID_BEAR,
                DIV_HID_BULL}
        assert all(f[f"div_{o}"] in divs
                   for o in ("srsi14", "smacd", "smoothed_RSI_diff"))

    def test_distances_on_controlled_series(self):
        asm = SignalFeatureAssembler()
        meds = [1, 2, 3, 2, 1, 2, 3, 4, 3]
        closes = [10, 10, 10, 10, 10, 10, 10, 10, 20]
        for m, cl in zip(meds, closes):
            asm.update(close=float(cl), high=float(cl + 1), low=float(cl - 1),
                       med_ema=float(m), long_ema=float(m),
                       longest_ema=float(m), atr=2.0)
        f = asm.features()
        # med peak at bar 7 (val 4), k=1 -> nearest pivot bar 7, current bar 8
        assert f["bars_since_med_ema_turn"] == 1
        assert not math.isnan(f["pdisp_med_ema"])
        assert f["pdisp_med_ema"] > 0


class TestSnapshotRestore:
    def test_branch_and_restore_identical_continuation(self):
        close, high, low, med, lng, lst, atr = _fixture(n=140)

        def feed(a, i):
            a.update(close=float(close[i]), high=float(high[i]),
                     low=float(low[i]), med_ema=float(med[i]),
                     long_ema=float(lng[i]), longest_ema=float(lst[i]),
                     atr=float(atr[i]))

        a1 = SignalFeatureAssembler()
        for i in range(80):
            feed(a1, i)
        snap = a1.snapshot()
        for i in range(80, 88):          # branch (must be discarded)
            feed(a1, i)
        a1.features()
        a1.restore(snap)
        feed(a1, 88)
        f1 = a1.features()

        a2 = SignalFeatureAssembler()
        for i in range(80):
            feed(a2, i)
        feed(a2, 88)                      # never branched
        f2 = a2.features()

        for k in f1:
            v1, v2 = f1[k], f2[k]
            if isinstance(v1, float) and isinstance(v2, float):
                assert (math.isnan(v1) and math.isnan(v2)) or v1 == v2
            else:
                assert v1 == v2
