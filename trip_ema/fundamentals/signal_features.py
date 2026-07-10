"""Signal feature assembler (spec v0.3 §6–§10). Pure orchestration.

Combines the three primitives into one per-bar feature dict:
  - structure_series.StructureSeriesEngine  -> the §4 oscillator/spread series
  - causal_pivots.CausalPivotTracker         -> per-member confirmed pivots
  - pattern_rules.structure_labels           -> HH/HL/LH/LL + composite

and derives the §8 distances (bars-since-turn, price displacement in
ATR28) and §10 divergence (price structure vs oscillator structure).

No DB, no I/O. Fed one bar at a time with the raw OHLC plus the already-
computed med/long/longest EMAs + ATR (from stock_indicator_state); call
`features()` at a signal bar to snapshot the derived vector. Resume is
per-ticker (re-walk from the first bar), so no snapshot/restore here.
"""
from __future__ import annotations

from collections import deque
from statistics import mean, median, pstdev

import math

from .causal_pivots import CausalPivotTracker
from .pattern_rules import (
    LABEL_1_HIGHER_VALLEY,
    STRUCT_UPTREND,
    LABEL_1_LOWER_TOP,
    LABEL_2_HIGHER_VALLEYS,
    LABEL_2_LOWER_TOPS,
    LABEL_NA,
    REL_HH,
    REL_HL,
    REL_LH,
    REL_LL,
    pattern_label,
    structure_labels,
)
from .structure_series import StructureSeriesEngine

_NAN = float("nan")


def _isnan(x: float | None) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


# (member name, series source key, confirmation window k). med/long/
# longest come from the input EMAs; the rest from the §4 series.
_MEMBERS: list[tuple[str, str, int]] = [
    ("med_ema", "med_ema", 1),
    ("long_ema", "long_ema", 1),
    ("longest_ema", "longest_ema", 1),
    ("srsi14", "srsi14", 2),
    ("smacd", "smacd", 2),
    ("sdosc", "sdosc", 1),
    ("smoothed_RSI_diff", "smoothed_RSI_diff", 2),
    ("ema_smoothed_RSI_diff", "ema_smoothed_RSI_diff", 2),
    ("ema_ema_diff_1", "ema_ema_diff_1", 2),
    ("ema_ema_diff_2", "ema_ema_diff_2", 2),
    ("ema_ema_diff_3", "ema_ema_diff_3", 2),
]

# oscillator members use native move-since-turn; price/EMA/spread use pct.
_OSC_MEMBERS = frozenset({
    "srsi14", "smacd", "sdosc",
    "smoothed_RSI_diff", "ema_smoothed_RSI_diff",
})
_BUY_LABELS = frozenset({LABEL_2_HIGHER_VALLEYS, LABEL_1_HIGHER_VALLEY})
_SELL_LABELS = frozenset({LABEL_2_LOWER_TOPS, LABEL_1_LOWER_TOP})

_DIST_KEYS = (("med", "med_ema"), ("long", "long_ema"),
              ("longest", "longest_ema"))
_NORM_SERIES = (
    "srsi14", "smacd", "sdosc",
    "smoothed_RSI_diff", "ema_smoothed_RSI_diff",
)
_WINDOW = 52

# §4 raw levels this assembler owns (snapshotted into the S-group).
_SERIES_KEYS = (
    "srsi14", "srsi28", "smoothed_RSI_diff", "ema_smoothed_RSI_diff",
    "smacd", "macd_hist", "sdosc",
    "ema_diff_1", "ema_diff_2", "ema_diff_3",
    "ema_ema_diff_1", "ema_ema_diff_2", "ema_ema_diff_3", "atr28",
)

# oscillators divergence is read against the price (med_ema) structure.
_DIV_OSC = ("srsi14", "smacd", "smoothed_RSI_diff")

DIV_NONE = "NONE"
DIV_REG_BEAR = "REG_BEAR"
DIV_REG_BULL = "REG_BULL"
DIV_HID_BEAR = "HID_BEAR"
DIV_HID_BULL = "HID_BULL"


def _divergence(
    p_peak: str, p_trough: str, o_peak: str, o_trough: str,
) -> str:
    """Price structure vs oscillator structure (spec §10)."""
    if p_peak == REL_HH and o_peak == REL_LH:
        return DIV_REG_BEAR
    if p_trough == REL_LL and o_trough == REL_HL:
        return DIV_REG_BULL
    if p_peak == REL_LH and o_peak == REL_HH:
        return DIV_HID_BEAR
    if p_trough == REL_HL and o_trough == REL_LL:
        return DIV_HID_BULL
    return DIV_NONE


def _slope(v1, v0, b1, b0):
    if v1 is None or v0 is None or b1 is None or b0 is None or b1 == b0:
        return None
    return (v1 - v0) / (b1 - b0)


def _div_strength_age(cls, p_trk, o_trk, cur_bar):
    """(strength, age) for a divergence class (spec §10): strength is the
    absolute slope gap between the price and oscillator legs over their
    paired pivots; age is bars since the price pivot that completed it."""
    if cls in (DIV_REG_BEAR, DIV_HID_BEAR):
        p_sl = _slope(p_trk.last_peak_value, p_trk.prev_peak_value,
                      p_trk.last_peak_bar, p_trk.prev_peak_bar)
        o_sl = _slope(o_trk.last_peak_value, o_trk.prev_peak_value,
                      o_trk.last_peak_bar, o_trk.prev_peak_bar)
        comp_bar = p_trk.last_peak_bar
    elif cls in (DIV_REG_BULL, DIV_HID_BULL):
        p_sl = _slope(p_trk.last_trough_value, p_trk.prev_trough_value,
                      p_trk.last_trough_bar, p_trk.prev_trough_bar)
        o_sl = _slope(o_trk.last_trough_value, o_trk.prev_trough_value,
                      o_trk.last_trough_bar, o_trk.prev_trough_bar)
        comp_bar = p_trk.last_trough_bar
    else:
        return _NAN, None
    if p_sl is None or o_sl is None:
        return _NAN, None
    age = cur_bar - comp_bar if comp_bar is not None else None
    return abs(p_sl - o_sl), age


def _z_pctrank(cur, buf):
    """(z-score, percentile-rank) of cur within buf (spec §9)."""
    if _isnan(cur) or len(buf) < 2:
        return _NAN, _NAN
    m = mean(buf)
    sd = pstdev(buf)
    z = (cur - m) / sd if sd > 0.0 else _NAN
    rank = sum(1 for x in buf if x <= cur) / len(buf)
    return z, rank


def _trend_r2(closes, n):
    """R-squared of close ~ bar over the last n closes (spec §12)."""
    ys = closes[-n:]
    m = len(ys)
    if m < 3:
        return _NAN
    mx = (m - 1) / 2.0
    my = sum(ys) / m
    sxx = sum((i - mx) ** 2 for i in range(m))
    sxy = sum((i - mx) * (y - my) for i, y in enumerate(ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return _NAN
    r = sxy / ((sxx ** 0.5) * (syy ** 0.5))
    return r * r


def _pctile(buf, q):
    """Linear-interpolated q-quantile of buf (0<=q<=1)."""
    xs = sorted(buf)
    if not xs:
        return None
    idx = q * (len(xs) - 1)
    lo = int(idx)
    frac = idx - lo
    if lo + 1 < len(xs):
        return xs[lo] + frac * (xs[lo + 1] - xs[lo])
    return xs[lo]


class SignalFeatureAssembler:
    """One instance per (ticker, timeframe); feed bars in order."""

    def __init__(self) -> None:
        self._series = StructureSeriesEngine()
        self._trk = {
            name: CausalPivotTracker(k=k) for name, _src, k in _MEMBERS
        }
        self._closes: list[float] = []
        self._vols: list[float] = []
        self._bar = -1
        self._latest: dict = {}
        self._mval: dict = {}
        self._mret: dict = {}
        self._nsucc: dict = {}
        self._lastdir: dict = {}
        self._legamps: dict = {
            name: deque(maxlen=52) for name, _s, _k in _MEMBERS
        }
        self._lbl_buy: dict = {name: LABEL_NA for name, _s, _k in _MEMBERS}
        self._lbl_sell: dict = {name: LABEL_NA for name, _s, _k in _MEMBERS}
        self._plabel: dict = {}
        self._distbuf: dict = {
            k: deque(maxlen=_WINDOW) for k, _e in _DIST_KEYS
        }
        self._normbuf: dict = {
            k: deque(maxlen=_WINDOW) for k in _NORM_SERIES
        }
        self._curdist: dict = {}
        self._abovemed: deque = deque(maxlen=_WINDOW)
        self._bwbuf: deque = deque(maxlen=_WINDOW)
        self._prev_squeeze = False
        self._last_regime = None
        self._regime_change_bar = None
        self._cur_bw = _NAN
        self._cur_squeeze = False
        self._cur_release = False
        self._cur_wir = None

    def update(
        self, *,
        close: float, high: float, low: float,
        med_ema: float | None, long_ema: float | None,
        longest_ema: float | None, atr: float | None,
        upper_outer: float | None = None,
        lower_outer: float | None = None,
        regime: int | None = None,
        volume: float | None = None,
    ) -> None:
        self._bar += 1
        self._closes.append(close)
        self._vols.append(volume if volume is not None else _NAN)
        s = self._series.update(
            close=close, high=high, low=low,
            med_ema=med_ema, long_ema=long_ema,
            longest_ema=longest_ema, atr=atr,
        )
        self._latest = s
        src = {"med_ema": med_ema, "long_ema": long_ema,
               "longest_ema": longest_ema, **s}
        self._mval = {}
        self._mret = {}
        for name, key, _k in _MEMBERS:
            self._mval[name] = src[key]
            trk = self._trk[name]
            ret = trk.update(self._bar, src[key])
            self._mret[name] = ret
            if ret["peak_confirmed"] or ret["trough_confirmed"]:
                if (trk.last_peak_value is not None
                        and trk.last_trough_value is not None):
                    self._legamps[name].append(
                        abs(trk.last_peak_value - trk.last_trough_value)
                    )
                if ret["peak_confirmed"]:
                    v, pv = trk.last_peak_value, trk.prev_peak_value
                else:
                    v, pv = trk.last_trough_value, trk.prev_trough_value
                newdir = None if pv is None else ("UP" if v > pv else "DOWN")
                if newdir is not None:
                    if newdir == self._lastdir.get(name):
                        self._nsucc[name] = self._nsucc.get(name, 0) + 1
                    else:
                        self._nsucc[name] = 1
                    self._lastdir[name] = newdir
            plbl = pattern_label(
                ret["trough_confirmed"], ret["peak_confirmed"], src[key],
                trk.last_trough_value, trk.prev_trough_value,
                trk.last_peak_value, trk.prev_peak_value,
                self._lbl_buy.get(name, LABEL_NA),
                self._lbl_sell.get(name, LABEL_NA),
                bars_since_last_buy=(
                    self._bar - trk.last_trough_bar
                    if trk.last_trough_bar is not None else None
                ),
                bars_since_last_sell=(
                    self._bar - trk.last_peak_bar
                    if trk.last_peak_bar is not None else None
                ),
            )
            self._plabel[name] = plbl
            if ret["trough_confirmed"] and plbl in _BUY_LABELS:
                self._lbl_buy[name] = plbl
            if ret["peak_confirmed"] and plbl in _SELL_LABELS:
                self._lbl_sell[name] = plbl

        self._curdist = {}
        ok_atr = atr is not None and not _isnan(atr) and atr > 0.0
        for k, ekey in _DIST_KEYS:
            ev = src.get(ekey)
            if ok_atr and ev is not None and not _isnan(ev):
                d = (close - ev) / atr
            else:
                d = _NAN
            self._curdist[k] = d
            if not _isnan(d):
                self._distbuf[k].append(d)
        for k in _NORM_SERIES:
            v = s.get(k, _NAN)
            if not _isnan(v):
                self._normbuf[k].append(v)
        if med_ema is not None and not _isnan(med_ema):
            self._abovemed.append(1 if close > med_ema else 0)

        if (upper_outer is not None and lower_outer is not None
                and not _isnan(upper_outer) and not _isnan(lower_outer)
                and close != 0.0):
            bw = (upper_outer - lower_outer) / close
        else:
            bw = _NAN
        self._cur_bw = bw
        if not _isnan(bw):
            self._bwbuf.append(bw)
        if not _isnan(bw) and len(self._bwbuf) >= 2:
            p20 = _pctile(self._bwbuf, 0.2)
            sq = bool(p20 is not None and bw < p20)
        else:
            sq = False
        self._cur_release = bool(self._prev_squeeze and not sq)
        self._cur_squeeze = sq
        self._prev_squeeze = sq
        if regime is not None:
            if regime != self._last_regime:
                self._regime_change_bar = self._bar
                self._last_regime = regime
            self._cur_wir = (
                self._bar - self._regime_change_bar
                if self._regime_change_bar is not None else None
            )
        else:
            self._cur_wir = None

    def snapshot(self) -> tuple:
        """Capture engine + per-member tracker state + the closes length,
        bar index, and last series dict — for branching at signal weeks.
        The closes list is not copied; restore() truncates it back."""
        return (
            self._series.snapshot(),
            {name: self._trk[name].snapshot() for name in self._trk},
            len(self._closes),
            self._bar,
            dict(self._latest),
            dict(self._mval),
            dict(self._mret),
            dict(self._nsucc),
            dict(self._lastdir),
            {n: tuple(d) for n, d in self._legamps.items()},
            dict(self._lbl_buy),
            dict(self._lbl_sell),
            dict(self._plabel),
            {k: tuple(d) for k, d in self._distbuf.items()},
            {k: tuple(d) for k, d in self._normbuf.items()},
            dict(self._curdist),
            tuple(self._abovemed),
            tuple(self._bwbuf),
            self._prev_squeeze,
            self._last_regime,
            self._regime_change_bar,
            self._cur_bw,
            self._cur_squeeze,
            self._cur_release,
            self._cur_wir,
        )

    def restore(self, snap: tuple) -> None:
        (series_snap, trk_snaps, n_closes, bar, latest,
         mval, mret, nsucc, lastdir, legamps,
         lbl_buy, lbl_sell, plabel,
         distbuf, normbuf, curdist, abovemed,
         bwbuf, prev_sq, last_reg, reg_bar,
         cur_bw, cur_sq, cur_rel, cur_wir) = snap
        self._series.restore(series_snap)
        for name, s in trk_snaps.items():
            self._trk[name].restore(s)
        del self._closes[n_closes:]
        del self._vols[n_closes:]
        self._bar = bar
        self._latest = dict(latest)
        self._mval = dict(mval)
        self._mret = dict(mret)
        self._nsucc = dict(nsucc)
        self._lastdir = dict(lastdir)
        self._legamps = {
            n: deque(v, maxlen=52) for n, v in legamps.items()
        }
        self._lbl_buy = dict(lbl_buy)
        self._lbl_sell = dict(lbl_sell)
        self._plabel = dict(plabel)
        self._distbuf = {
            k: deque(v, maxlen=_WINDOW) for k, v in distbuf.items()
        }
        self._normbuf = {
            k: deque(v, maxlen=_WINDOW) for k, v in normbuf.items()
        }
        self._curdist = dict(curdist)
        self._abovemed = deque(abovemed, maxlen=_WINDOW)
        self._bwbuf = deque(bwbuf, maxlen=_WINDOW)
        self._prev_squeeze = prev_sq
        self._last_regime = last_reg
        self._regime_change_bar = reg_bar
        self._cur_bw = cur_bw
        self._cur_squeeze = cur_sq
        self._cur_release = cur_rel
        self._cur_wir = cur_wir

    def features(self) -> dict:
        """The derived feature vector at the current bar (call at a
        signal bar). Structure + distances + divergence + §4 levels;
        merged with existing enrichment + lookups downstream."""
        s = self._latest
        atr28 = s.get("atr28", _NAN)
        out: dict = {k: s.get(k, _NAN) for k in _SERIES_KEYS}

        rels: dict[str, tuple[str, str]] = {}
        for name, _key, _k in _MEMBERS:
            trk = self._trk[name]
            prel, trel, comp = structure_labels(
                last_peak_value=trk.last_peak_value,
                prev_peak_value=trk.prev_peak_value,
                last_trough_value=trk.last_trough_value,
                prev_trough_value=trk.prev_trough_value,
            )
            rels[name] = (prel, trel)
            out[f"{name}_structure"] = comp
            npb = trk.nearest_pivot_bar()
            out[f"bars_since_{name}_turn"] = (
                self._bar - npb if npb is not None else None
            )
            if npb is not None and not _isnan(atr28) and atr28 > 0.0:
                out[f"pdisp_{name}"] = (
                    (self._closes[self._bar] - self._closes[npb]) / atr28
                )
            else:
                out[f"pdisp_{name}"] = _NAN

            lpb, ltb = trk.last_peak_bar, trk.last_trough_bar
            lpv, ltv = trk.last_peak_value, trk.last_trough_value
            out[f"bars_since_{name}_peak"] = (
                self._bar - lpb if lpb is not None else None
            )
            out[f"bars_since_{name}_trough"] = (
                self._bar - ltb if ltb is not None else None
            )
            if (lpv is not None and ltv is not None
                    and lpb is not None and ltb is not None
                    and lpb != ltb):
                amp = abs(lpv - ltv)
                out[f"{name}_leg_amplitude"] = amp
                out[f"{name}_leg_velocity"] = amp / abs(lpb - ltb)
            else:
                out[f"{name}_leg_amplitude"] = _NAN
                out[f"{name}_leg_velocity"] = _NAN

            ret = self._mret.get(name) or {}
            out[f"{name}_turn_buy"] = bool(ret.get("trough_confirmed"))
            out[f"{name}_turn_sell"] = bool(ret.get("peak_confirmed"))
            mv = self._mval.get(name, _NAN)
            origin = (lpv if npb == lpb else ltv) if npb is not None else None
            if (npb is not None and not _isnan(mv)
                    and origin is not None and not _isnan(origin)):
                if name in _OSC_MEMBERS:
                    out[f"move_since_{name}_turn"] = mv - origin
                elif origin != 0.0:
                    out[f"move_since_{name}_turn"] = (mv - origin) / abs(origin)
                else:
                    out[f"move_since_{name}_turn"] = _NAN
            else:
                out[f"move_since_{name}_turn"] = _NAN

            out[f"{name}_n_successive"] = self._nsucc.get(name, 0)
            amps = self._legamps.get(name)
            if (npb is not None and not _isnan(mv) and origin is not None
                    and not _isnan(origin) and amps):
                med_amp = median(amps)
                out[f"{name}_leg_maturity"] = (
                    abs(mv - origin) / med_amp if med_amp > 0 else _NAN
                )
            else:
                out[f"{name}_leg_maturity"] = _NAN
            out[f"{name}_pattern_label"] = self._plabel.get(name, LABEL_NA)

        p_peak, p_trough = rels["med_ema"]
        p_trk = self._trk["med_ema"]
        for osc in _DIV_OSC:
            o_peak, o_trough = rels[osc]
            cls = _divergence(p_peak, p_trough, o_peak, o_trough)
            out[f"div_{osc}_class"] = cls
            strength, age = _div_strength_age(
                cls, p_trk, self._trk[osc], self._bar
            )
            out[f"div_{osc}_strength"] = strength
            out[f"div_{osc}_age"] = age

        for k, _e in _DIST_KEYS:
            out[f"dist_to_{k}_in_hl_units"] = self._curdist.get(k, _NAN)
            buf = self._distbuf[k]
            out[f"mean_dist_to_{k}_in_hl_units"] = (
                mean(buf) if buf else _NAN
            )
        for k in _NORM_SERIES:
            z, rank = _z_pctrank(self._latest.get(k, _NAN), self._normbuf[k])
            out[f"z_{k}"] = z
            out[f"pctrank_{k}"] = rank

        out["extension_z"] = _z_pctrank(
            self._curdist.get("longest", _NAN), self._distbuf["longest"]
        )[0]
        dmed = self._distbuf["med"]
        out["chop"] = pstdev(dmed) if len(dmed) >= 2 else _NAN
        out["frac_above_med"] = (
            mean(self._abovemed) if self._abovemed else _NAN
        )
        out["trend_r2"] = _trend_r2(self._closes, _WINDOW)
        out["ema_agreement"] = sum(
            1 for x in ("med_ema", "long_ema", "longest_ema")
            if out.get(f"{x}_structure") == STRUCT_UPTREND
        )
        out["bw"] = self._cur_bw
        out["squeeze"] = self._cur_squeeze
        out["squeeze_release"] = self._cur_release
        out["weeks_in_regime"] = self._cur_wir

        tb = self._trk["med_ema"].last_trough_bar
        if tb is not None and 0 <= tb < len(self._vols):
            vtb = self._vols[tb]
            win = [v for v in self._vols[max(0, tb - 19): tb + 1]
                   if not _isnan(v)]
            sma = mean(win) if win else _NAN
            if not _isnan(vtb) and not _isnan(sma) and sma > 0.0:
                out["vol_at_pivot"] = vtb / sma
            else:
                out["vol_at_pivot"] = _NAN
        else:
            out["vol_at_pivot"] = _NAN
        vap = out["vol_at_pivot"]
        out["accumulation"] = (not _isnan(vap)) and vap > 1.5
        return out
