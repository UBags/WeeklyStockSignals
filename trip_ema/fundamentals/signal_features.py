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

import math

from .causal_pivots import CausalPivotTracker
from .pattern_rules import (
    REL_HH,
    REL_HL,
    REL_LH,
    REL_LL,
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
        for name, key, _k in _MEMBERS:
            self._trk[name].update(self._bar, src[key])

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

        p_peak, p_trough = rels["med_ema"]
        for osc in _DIV_OSC:
            o_peak, o_trough = rels[osc]
            out[f"div_{osc}"] = _divergence(
                p_peak, p_trough, o_peak, o_trough,
            )
        return out
