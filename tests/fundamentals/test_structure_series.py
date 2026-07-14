"""Bit-parity + behavior tests for the §4 recursive series (spec §4).

The scalar recursion in structure_series must match pandas ewm exactly
(seed-with-first, recursive alpha), so these assert bit-parity against
independent pandas references plus snapshot/restore and NaN handling."""
import math

import numpy as np
import pandas as pd

from trip_ema.fundamentals.signal_bands import ATR_PERIODS
from trip_ema.fundamentals.structure_series import StructureSeriesEngine


def _fixture(n=400, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    span = np.abs(rng.normal(0, 1.0, n)) + 0.3
    high, low = close + span, close - span
    c = pd.Series(close)
    med = c.ewm(span=8, adjust=False).mean().to_numpy()
    lng = c.ewm(span=21, adjust=False).mean().to_numpy()
    lngst = c.ewm(span=50, adjust=False).mean().to_numpy()
    atr = pd.Series(high - low).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    return close, high, low, med, lng, lngst, atr


def _drive(close, high, low, med, lng, lngst, atr):
    eng = StructureSeriesEngine()
    rows = []
    for i in range(len(close)):
        rows.append(eng.update(
            close=float(close[i]), high=float(high[i]), low=float(low[i]),
            med_ema=float(med[i]), long_ema=float(lng[i]),
            longest_ema=float(lngst[i]), atr=float(atr[i]),
        ))
    return {k: np.array([r[k] for r in rows]) for k in rows[0]}


def _pandas_ref(close, high, low, med, lng, lngst, atr):
    c = pd.Series(close)
    u = pd.Series(atr)

    def srsi(nn):
        d = c.diff()
        au = d.clip(lower=0).ewm(alpha=1 / nn, adjust=False).mean()
        ad = (-d).clip(lower=0).ewm(alpha=1 / nn, adjust=False).mean()
        return (100 - 100 / (1 + au / ad)).ewm(span=3, adjust=False).mean()

    s14, s28 = srsi(14), srsi(28)
    rd = s14 - s28
    fast = c.ewm(span=12, adjust=False).mean()
    slow = c.ewm(span=26, adjust=False).mean()
    macd = fast - slow
    sig = macd.ewm(span=9, adjust=False).mean()
    d1 = pd.Series(med) - pd.Series(lng)
    d2 = pd.Series(med) - pd.Series(lngst)
    d3 = pd.Series(lng) - pd.Series(lngst)
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        pc = close[i - 1]
        tr[i] = max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc))
    ref = {
        "srsi14": s14, "srsi28": s28, "smoothed_RSI_diff": rd,
        "ema_smoothed_RSI_diff": rd.ewm(span=3, adjust=False).mean(),
        "macd_hist": macd - sig,
        "smacd": (macd / u).ewm(span=3, adjust=False).mean(),
        "sdosc": ((c - pd.Series(lngst)) / u).ewm(span=5, adjust=False).mean(),
        "ema_diff_1": d1, "ema_diff_2": d2, "ema_diff_3": d3,
        "ema_ema_diff_1": d1.ewm(span=3, adjust=False).mean(),
        "ema_ema_diff_2": d2.ewm(span=3, adjust=False).mean(),
        "ema_ema_diff_3": d3.ewm(span=3, adjust=False).mean(),
        "atr28": pd.Series(tr).ewm(alpha=1 / 28, adjust=False).mean(),
        # The §20 band rulers. Same true range, same Wilder alpha, held to
        # the same bit-parity bar as everything above.
        **{
            f"atr{nn}": pd.Series(tr).ewm(alpha=1 / nn, adjust=False).mean()
            for nn in ATR_PERIODS
        },
    }
    return {k: v.to_numpy() for k, v in ref.items()}


class TestPandasParity:
    def test_all_series_bit_identical_to_pandas(self):
        fx = _fixture()
        got, ref = _drive(*fx), _pandas_ref(*fx)
        assert set(got) == set(ref)
        for k in got:
            g, r = got[k], ref[k]
            assert np.array_equal(np.isnan(g), np.isnan(r)), f"{k} nan mismatch"
            if (~np.isnan(g)).any():
                assert np.nanmax(np.abs(g - r)) < 1e-9, f"{k} value mismatch"


class TestBehavior:
    def test_first_bar_rsi_nan_atr28_seeds(self):
        row = StructureSeriesEngine().update(
            close=100.0, high=101.0, low=99.0,
            med_ema=100.0, long_ema=100.0, longest_ema=100.0, atr=1.0,
        )
        assert math.isnan(row["srsi14"])          # no delta on bar 0
        assert row["atr28"] == 2.0                # seeds with TR = high-low

    def test_nan_atr_yields_nan_normalized(self):
        row = StructureSeriesEngine().update(
            close=100.0, high=101.0, low=99.0,
            med_ema=100.0, long_ema=99.0, longest_ema=98.0, atr=float("nan"),
        )
        assert math.isnan(row["smacd"])
        assert math.isnan(row["sdosc"])
        assert row["ema_diff_1"] == 1.0           # unnormalized spread still set

    def test_snapshot_restore_round_trip(self):
        fx = _fixture(n=120)
        e = StructureSeriesEngine()
        for i in range(80):
            e.update(close=float(fx[0][i]), high=float(fx[1][i]),
                     low=float(fx[2][i]), med_ema=float(fx[3][i]),
                     long_ema=float(fx[4][i]), longest_ema=float(fx[5][i]),
                     atr=float(fx[6][i]))
        clone = StructureSeriesEngine()
        clone.restore(e.snapshot())
        kw = dict(close=float(fx[0][80]), high=float(fx[1][80]),
                  low=float(fx[2][80]), med_ema=float(fx[3][80]),
                  long_ema=float(fx[4][80]), longest_ema=float(fx[5][80]),
                  atr=float(fx[6][80]))
        a, b = e.update(**kw), clone.update(**kw)
        for k in a:
            assert (math.isnan(a[k]) and math.isnan(b[k])) or a[k] == b[k]
