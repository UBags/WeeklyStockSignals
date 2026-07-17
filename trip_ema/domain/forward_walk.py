"""Pure per-signal forward-walk geometry (spec 02_signal_forward_walk_labels).

`walk_signal` is the correctness core: given the entry fill, the adjusted daily
bars for the window ``[entry … N]`` (entry bar first, window-end bar last), and
the trade side, it measures how the trade played out — gain/loss rungs touched,
MAE/MFE excursions, realized + excess returns, and in-window path statistics.
It is PURE: no I/O, no DB, no clock. The runner owns bar loading, industry/market
series, window censoring, and persistence.

Bar indexing (spec, locked): the signal day is bar 0 and the entry day is bar 1,
so a touch on the entry bar has distance 0. ``bars_to_* = hit_list_index`` where
``hit_list_index`` is the 0-based position within ``bars`` (which starts at the
entry bar). ``-1`` = never touched within the window.

Intrabar, direction-aware touch (spec, locked):
  * LONG  — gains on each bar's HIGH, losses on each bar's LOW;
  * SHORT — gains on each bar's LOW,  losses on each bar's HIGH.
Each rung is an independent "ever touched within window" flag, so intrabar
ordering never matters.
"""
from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

LONG = "LONG"
SHORT = "SHORT"

# Relative tolerance for threshold touches. entry_open*(1+frac) is not exactly
# representable (100*1.10 == 110.00000000000001), so an exact round-number touch
# would otherwise miss. 1e-9 relative is far tighter than any real tick, so it
# admits genuine touches without ever manufacturing a false one.
_TOL = 1e-9


def _reached_up(x: float, level: float) -> bool:
    """True when x has risen to at least `level` (level > 0)."""
    return x >= level - level * _TOL


def _reached_down(x: float, level: float) -> bool:
    """True when x has fallen to at most `level` (level > 0)."""
    return x <= level + level * _TOL

# Rung definitions — the runtime single source of truth. Labels spell the column
# tokens (hit_gain_<label>_pct / bars_to_gain_<label>_pct). Migration 0055
# spells these literally and independently; the cross-check tripwire in
# tests/domain/test_forward_walk.py fails if they drift.
GAIN_RUNGS: list[tuple[str, float]] = [
    (str(n), n / 100.0)
    for n in (1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31, 33,
              35, 50, 75, 100)
]
LOSS_RUNGS: list[tuple[str, float]] = [
    ("5", 0.05),
    ("7_and_half", 0.075),
    ("10", 0.10),
    ("12_and_half", 0.125),
    ("15", 0.15),
    ("20", 0.20),
]


@dataclass(frozen=True, slots=True)
class WalkBar:
    """One adjusted daily bar in the window. All prices are corporate-action
    adjusted (the walk runs on the corrected series, never raw yfinance)."""
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class ForwardWalkResult:
    side: str
    entry_open: float
    exit_close: float
    bars_in_window: int
    realized_ret: float
    # rung maps keyed by the rung label ("1".."100" / "5".."20"/"7_and_half"…)
    hit_gain: dict[str, bool]
    bars_to_gain: dict[str, int]
    hit_loss: dict[str, bool]
    bars_to_loss: dict[str, int]
    ever_in_profit: bool
    mae_pct: float | None
    mae_bar: int
    mfe_pct: float | None
    mfe_bar: int
    mae_before_mfe: bool
    excess_ret_industry: float | None
    excess_ret_market: float | None
    mfe_atr20: float | None
    mae_atr20: float | None
    time_underwater_bars: int
    slope_per_bar: float | None
    realized_vol: float | None
    sharpe_in_window: float | None
    capture_ratio: float | None


def _ols_slope(ys: Sequence[float]) -> float | None:
    """OLS slope of ys against bar index 0..m-1. None when m < 2."""
    m = len(ys)
    if m < 2:
        return None
    mean_x = (m - 1) / 2.0
    mean_y = sum(ys) / m
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(ys))
    den = sum((i - mean_x) ** 2 for i in range(m))
    if den == 0:
        return None
    return num / den


def walk_signal(
    entry_open: float,
    bars: Sequence[WalkBar],
    side: str,
    *,
    atr20: float | None = None,
    industry_ret: float | None = None,
    market_ret: float | None = None,
) -> ForwardWalkResult:
    """Measure the trade over ``bars`` = the window ``[entry … N]`` inclusive.

    `entry_open` is the entry fill (the adjusted open of the entry bar).
    `atr20`, `industry_ret`, `market_ret` are optional context supplied by the
    runner (None → the derived columns are None).
    """
    if side not in (LONG, SHORT):
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")
    if entry_open is None or entry_open <= 0:
        raise ValueError(f"entry_open must be > 0, got {entry_open!r}")
    if not bars:
        raise ValueError("walk_signal requires at least the entry bar")

    is_long = side == LONG
    m = len(bars)
    exit_close = bars[-1].close
    closes = [b.close for b in bars]

    # --- realized directional return -------------------------------------
    if is_long:
        realized_ret = exit_close / entry_open - 1.0
    else:
        realized_ret = entry_open / exit_close - 1.0

    # --- rungs: first-touch bar index per rung ---------------------------
    def _first_touch(frac: float, *, gain: bool) -> int:
        # Resolve which price level and which bar extreme this rung watches.
        if is_long:
            if gain:
                level = entry_open * (1.0 + frac)
                return next(
                    (i for i, b in enumerate(bars) if _reached_up(b.high, level)),
                    -1,
                )
            level = entry_open * (1.0 - frac)
            return next(
                (i for i, b in enumerate(bars) if _reached_down(b.low, level)),
                -1,
            )
        # short
        if gain:
            level = entry_open * (1.0 - frac)
            return next(
                (i for i, b in enumerate(bars) if _reached_down(b.low, level)),
                -1,
            )
        level = entry_open * (1.0 + frac)
        return next(
            (i for i, b in enumerate(bars) if _reached_up(b.high, level)),
            -1,
        )

    hit_gain: dict[str, bool] = {}
    bars_to_gain: dict[str, int] = {}
    for label, frac in GAIN_RUNGS:
        idx = _first_touch(frac, gain=True)
        bars_to_gain[label] = idx
        hit_gain[label] = idx != -1

    hit_loss: dict[str, bool] = {}
    bars_to_loss: dict[str, int] = {}
    for label, frac in LOSS_RUNGS:
        idx = _first_touch(frac, gain=False)
        bars_to_loss[label] = idx
        hit_loss[label] = idx != -1

    # --- excursions (favorable / adverse), direction-aware ---------------
    # Entry bar guarantees favorable>=0 and adverse>=0 (high>=open, low<=open),
    # so MFE and MAE are always non-negative magnitudes with a defined bar.
    if is_long:
        favorable = [(b.high - entry_open) / entry_open for b in bars]
        adverse = [(entry_open - b.low) / entry_open for b in bars]
    else:
        favorable = [(entry_open - b.low) / entry_open for b in bars]
        adverse = [(b.high - entry_open) / entry_open for b in bars]

    mfe_pct = max(favorable)
    mfe_bar = favorable.index(mfe_pct)
    mae_pct = max(adverse)
    mae_bar = adverse.index(mae_pct)
    ever_in_profit = mfe_pct > 0.0
    mae_before_mfe = mae_bar < mfe_bar

    # --- ATR(20)-normalized excursions -----------------------------------
    if atr20 not in (None, 0):
        mfe_atr20 = mfe_pct * entry_open / atr20
        mae_atr20 = mae_pct * entry_open / atr20
    else:
        mfe_atr20 = None
        mae_atr20 = None

    # --- time underwater: bars whose CLOSE is adverse vs entry -----------
    if is_long:
        time_underwater_bars = sum(1 for c in closes if c < entry_open)
    else:
        time_underwater_bars = sum(1 for c in closes if c > entry_open)

    # --- path statistics --------------------------------------------------
    slope = _ols_slope(closes)
    slope_per_bar = None if slope is None else slope / entry_open

    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, m)]
    if len(rets) >= 2:
        realized_vol = statistics.stdev(rets)
        sharpe_in_window = (
            statistics.fmean(rets) / realized_vol if realized_vol > 0 else None
        )
    else:
        realized_vol = None
        sharpe_in_window = None

    # capture: how much of the best favorable move the exit kept (<= 1)
    capture_ratio = realized_ret / mfe_pct if mfe_pct > 0 else None

    # --- excess returns ---------------------------------------------------
    excess_ret_industry = (
        None if industry_ret is None else realized_ret - industry_ret
    )
    excess_ret_market = (
        None if market_ret is None else realized_ret - market_ret
    )

    return ForwardWalkResult(
        side=side,
        entry_open=entry_open,
        exit_close=exit_close,
        bars_in_window=m,
        realized_ret=realized_ret,
        hit_gain=hit_gain,
        bars_to_gain=bars_to_gain,
        hit_loss=hit_loss,
        bars_to_loss=bars_to_loss,
        ever_in_profit=ever_in_profit,
        mae_pct=mae_pct,
        mae_bar=mae_bar,
        mfe_pct=mfe_pct,
        mfe_bar=mfe_bar,
        mae_before_mfe=mae_before_mfe,
        excess_ret_industry=excess_ret_industry,
        excess_ret_market=excess_ret_market,
        mfe_atr20=mfe_atr20,
        mae_atr20=mae_atr20,
        time_underwater_bars=time_underwater_bars,
        slope_per_bar=slope_per_bar,
        realized_vol=realized_vol,
        sharpe_in_window=sharpe_in_window,
        capture_ratio=capture_ratio,
    )
