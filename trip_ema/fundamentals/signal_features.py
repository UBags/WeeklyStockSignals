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
from statistics import median

import math

from .causal_pivots import CausalPivotTracker
from .pattern_rules import (
    LABEL_1_HIGHER_VALLEY,
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


class SignalFeatureAssembler:
    """One instance per (ticker, timeframe); feed bars in order."""

    def __init__(self) -> None:
        self._series = StructureSeriesEngine()
        self._trk = {
            name: CausalPivotTracker(k=k) for name, _src, k in _MEMBERS
        }
        self._closes: list[float] = []
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

    def update(
        self, *,
        close: float, high: float, low: float,
        med_ema: float | None, long_ema: float | None,
        longest_ema: float | None, atr: float | None,
    ) -> None:
        self._bar += 1
        self._closes.append(close)
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
        )

    def restore(self, snap: tuple) -> None:
        (series_snap, trk_snaps, n_closes, bar, latest,
         mval, mret, nsucc, lastdir, legamps,
         lbl_buy, lbl_sell, plabel) = snap
        self._series.restore(series_snap)
        for name, s in trk_snaps.items():
            self._trk[name].restore(s)
        del self._closes[n_closes:]
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
        for osc in _DIV_OSC:
            o_peak, o_trough = rels[osc]
            out[f"div_{osc}"] = _divergence(
                p_peak, p_trough, o_peak, o_trough,
            )
        return out
