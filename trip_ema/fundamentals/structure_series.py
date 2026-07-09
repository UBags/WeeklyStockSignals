"""§4 enrichment series — pure, recursive, causal (spec v0.3 §4).

Scalar state-carry computers for the smoothed-RSI / MACD / detrended /
EMA-spread families + atr28. No pandas, no I/O — same philosophy as
`pattern_rules`: the numeric layer differs between walkers, the recursion
lives here once.

Fed one bar at a time via `update(...)`, given the raw close/high/low and
the *already-computed* med/long/longest EMAs + ATR (from
stock_indicator_state) — so the three volume-weighted EMAs are NOT
recomputed here (no duplication with StatefulIndicatorEngine). Carries
O(1) state; `snapshot()`/`restore()` mirror the engine's resume pattern.

Seeding matches the codebase convention (pine_ema = ewm(span, adjust=
False); pine_rma = ewm(alpha=1/n, adjust=False)): seed with the first
non-NaN value, then apply the recursive alpha. Leading NaNs are skipped
(state stays None until the first real value), matching pandas ewm.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

_NAN = float("nan")


def _isnan(x: float | None) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _ema_step(prev: float | None, x: float | None, span: int) -> float | None:
    """EMA update, alpha = 2/(span+1). Seeds with the first non-NaN x;
    a NaN input leaves the state unchanged (skip, like pandas ewm)."""
    if _isnan(x):
        return prev
    if prev is None:
        return float(x)
    a = 2.0 / (span + 1.0)
    return a * float(x) + (1.0 - a) * prev


def _rma_step(prev: float | None, x: float | None, n: int) -> float | None:
    """Wilder RMA update, alpha = 1/n. Same seeding/NaN rule as _ema_step."""
    if _isnan(x):
        return prev
    if prev is None:
        return float(x)
    a = 1.0 / n
    return a * float(x) + (1.0 - a) * prev


def _out(x: float | None) -> float:
    """None -> NaN for emission (state stays None internally)."""
    return _NAN if x is None else x


def _rsi_from_avgs(au: float | None, ad: float | None) -> float:
    """RSI from Wilder avg-up / avg-down. Matches numpy semantics:
    positive/0 -> RSI 100; 0/0 -> NaN."""
    if au is None or ad is None:
        return _NAN
    if ad == 0.0:
        return 100.0 if au > 0.0 else _NAN
    return 100.0 - 100.0 / (1.0 + au / ad)


@dataclass
class _RSIState:
    """Wilder avg-up / avg-down + EMA3-smoothed RSI, for one RSI period."""
    au: float | None = None
    ad: float | None = None
    srsi: float | None = None

    def snapshot(self) -> tuple:
        return (self.au, self.ad, self.srsi)

    def restore(self, s: tuple) -> None:
        self.au, self.ad, self.srsi = s


@dataclass
class StructureSeriesEngine:
    """Incremental computer for the §4 enrichment series. One instance per
    (ticker, timeframe); feed bars in order via `update`."""

    _prev_close: float | None = None
    _rsi14: _RSIState = field(default_factory=_RSIState)
    _rsi28: _RSIState = field(default_factory=_RSIState)
    _ema_srsi_diff: float | None = None       # ema_smoothed_RSI_diff
    _macd_fast: float | None = None           # EMA12(close)
    _macd_slow: float | None = None           # EMA26(close)
    _macd_sig: float | None = None            # EMA9(macd line)
    _smacd: float | None = None               # EMA3(macd line / atr)
    _sdosc: float | None = None               # EMA5((close-longest)/atr)
    _ee_diff: list = field(default_factory=lambda: [None, None, None])
    _atr28: float | None = None               # Wilder RMA of TR, n=28
    _atr28_prev_close: float | None = None

    # -------------------- snapshot / restore -------------------- #

    def snapshot(self) -> tuple:
        return (
            self._prev_close,
            self._rsi14.snapshot(), self._rsi28.snapshot(),
            self._ema_srsi_diff,
            self._macd_fast, self._macd_slow, self._macd_sig, self._smacd,
            self._sdosc,
            tuple(self._ee_diff),
            self._atr28, self._atr28_prev_close,
        )

    def restore(self, s: tuple) -> None:
        (self._prev_close, r14, r28, self._ema_srsi_diff,
         self._macd_fast, self._macd_slow, self._macd_sig, self._smacd,
         self._sdosc, ee, self._atr28, self._atr28_prev_close) = s
        self._rsi14.restore(r14)
        self._rsi28.restore(r28)
        self._ee_diff = list(ee)

    # -------------------- per-bar advance -------------------- #

    def _step_rsi(self, st: _RSIState, up: float | None, dn: float | None,
                  n: int) -> float:
        """Advance one RSI period, return its EMA3-smoothed value."""
        st.au = _rma_step(st.au, up, n)
        st.ad = _rma_step(st.ad, dn, n)
        rsi = _rsi_from_avgs(st.au, st.ad)
        st.srsi = _ema_step(st.srsi, rsi, 3)
        return _out(st.srsi)

    def _true_range_28(self, high: float, low: float, close: float) -> float:
        pc = self._atr28_prev_close
        if pc is None:
            return high - low  # first bar: prev_close undefined
        return max(high - low, abs(high - pc), abs(low - pc))

    def update(
        self, *,
        close: float, high: float, low: float,
        med_ema: float | None, long_ema: float | None,
        longest_ema: float | None, atr: float | None,
    ) -> dict:
        """Advance one bar; return the §4 series for this bar. `atr` is the
        engine's ATR (state), used as the hl-unit normalizer for smacd and
        sdosc — matching the existing dist_to_*_in_hl_units convention."""
        # up / down move (no delta on the first bar -> state unchanged).
        if self._prev_close is None:
            up = dn = _NAN
        else:
            d = close - self._prev_close
            up = max(d, 0.0)
            dn = max(-d, 0.0)

        srsi14 = self._step_rsi(self._rsi14, up, dn, 14)
        srsi28 = self._step_rsi(self._rsi28, up, dn, 28)
        rsi_diff = (srsi14 - srsi28
                    if not (_isnan(srsi14) or _isnan(srsi28)) else _NAN)
        self._ema_srsi_diff = _ema_step(self._ema_srsi_diff, rsi_diff, 3)

        # MACD on close.
        self._macd_fast = _ema_step(self._macd_fast, close, 12)
        self._macd_slow = _ema_step(self._macd_slow, close, 26)
        macd = (self._macd_fast - self._macd_slow
                if not (self._macd_fast is None or self._macd_slow is None)
                else _NAN)
        self._macd_sig = _ema_step(self._macd_sig, macd, 9)
        macd_hist = (macd - self._macd_sig
                     if not (_isnan(macd) or self._macd_sig is None)
                     else _NAN)
        u = atr if (not _isnan(atr) and atr > 0.0) else _NAN
        nmacd = macd / u if not (_isnan(macd) or _isnan(u)) else _NAN
        self._smacd = _ema_step(self._smacd, nmacd, 3)

        # Detrended oscillator = EMA5((close - longest_ema)/atr).
        dosc = ((close - longest_ema) / u
                if not (_isnan(longest_ema) or _isnan(u)) else _NAN)
        self._sdosc = _ema_step(self._sdosc, dosc, 5)

        # EMA spreads + their EMA3 smoothings.
        if not (_isnan(med_ema) or _isnan(long_ema) or _isnan(longest_ema)):
            raw = [med_ema - long_ema, med_ema - longest_ema,
                   long_ema - longest_ema]
        else:
            raw = [_NAN, _NAN, _NAN]
        for i in range(3):
            self._ee_diff[i] = _ema_step(self._ee_diff[i], raw[i], 3)

        # ATR28 (Wilder RMA of true range, n=28).
        tr = self._true_range_28(high, low, close)
        self._atr28 = _rma_step(self._atr28, tr, 28)
        self._atr28_prev_close = close

        self._prev_close = close

        return {
            "srsi14": srsi14,
            "srsi28": srsi28,
            "smoothed_RSI_diff": _out(rsi_diff),
            "ema_smoothed_RSI_diff": _out(self._ema_srsi_diff),
            "smacd": _out(self._smacd),
            "macd_hist": _out(macd_hist),
            "sdosc": _out(self._sdosc),
            "ema_diff_1": _out(raw[0]),
            "ema_diff_2": _out(raw[1]),
            "ema_diff_3": _out(raw[2]),
            "ema_ema_diff_1": _out(self._ee_diff[0]),
            "ema_ema_diff_2": _out(self._ee_diff[1]),
            "ema_ema_diff_3": _out(self._ee_diff[2]),
            "atr28": _out(self._atr28),
        }
