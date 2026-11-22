"""Render the README indicator chart using trip_ema's own indicator code.

Input is SYNTHETIC weekly OHLCV (seeded random walk with drifting trend
phases) — no market data. Every line, band, regime colour and signal
marker is produced by the production functions:

    compute_indicator_series  -> triple VW-EMA stack, Wilder ATR, bands
    _compute_regime_series    -> the canonical regime state machine
    DetermineSignal           -> regime transition -> BUY/SELL/EXIT

Usage (from repo root):
    python scripts/render_readme_chart.py docs/images/indicator_example.png
"""
import sys
from pathlib import Path

# Run from a plain checkout without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from trip_ema.domain.determine_signal import DetermineSignal
from trip_ema.domain.state import ChangeSignal
from trip_ema.fundamentals.indicator_primitives import (
    _compute_regime_series,
    compute_indicator_series,
)

out_path = sys.argv[1]
rng = np.random.default_rng(7)

# ---- synthetic weekly bars: up-trend, chop, down-trend, recovery ----
phases = [(40, 0.012), (25, 0.000), (35, -0.011), (20, 0.002), (40, 0.010)]
drift = np.concatenate([np.full(n, d) for n, d in phases])
n = len(drift)
rets = drift + rng.normal(0, 0.028, n)
close = 100 * np.exp(np.cumsum(rets))
open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.004, n))
high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.015, n)))
low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.015, n)))
vol = rng.lognormal(13, 0.35, n)
idx = pd.date_range("2022-01-03", periods=n, freq="W-MON")
c, hi_s, lo_s, v = (pd.Series(x, index=idx) for x in (close, high, low, vol))

# ---- production indicator + regime + signal rule ----
s = compute_indicator_series(c, v, hi_s, lo_s)
regime = _compute_regime_series(c, s)
signals = []
for i in range(1, n):
    sig = DetermineSignal.determine_signal(int(regime.iloc[i - 1]), int(regime.iloc[i]))
    if sig is not ChangeSignal.NO_CHANGE:
        signals.append((idx[i], c.iloc[i], sig))

# ---- plot ----
fig, ax = plt.subplots(figsize=(13, 6.2), dpi=110)
fig.patch.set_facecolor("white")
w = pd.Timedelta(days=4)
for t, o, hi, lo, cl in zip(idx, open_, high, low, close, strict=True):
    col = "#26a69a" if cl >= o else "#ef5350"
    ax.vlines(t, lo, hi, color=col, lw=0.8, zorder=2)
    ax.add_patch(plt.Rectangle((t - w / 2, min(o, cl)), w, abs(cl - o) or 0.01,
                               color=col, zorder=3))

shade = {1: "#c8e6c9", -1: "#ffcdd2", 0: None}
for i in range(n - 1):
    col = shade[int(regime.iloc[i])]
    if col:
        ax.axvspan(idx[i] - w, idx[i + 1] - w, color=col, alpha=0.45, lw=0, zorder=0)

ax.plot(idx, s.emas.med, color="#1e88e5", lw=1.6, label="Med VW-EMA (8/4/4)")
ax.plot(idx, s.emas.long, color="#8e24aa", lw=1.6, label="Long VW-EMA (15/4/4/3)")
ax.plot(idx, s.emas.longest, color="#f57c00", lw=1.8, label="Longest VW-EMA (28/7/5/3)")
b = s.bands
ax.plot(idx, b.upper_inner, color="#2e7d32", lw=0.9, ls="--", label="Inner band (±0.2 ATR)")
ax.plot(idx, b.lower_inner, color="#c62828", lw=0.9, ls="--")
ax.fill_between(idx, b.lower_mid, b.upper_mid, color="#90a4ae", alpha=0.10,
                label="Mid band envelope (±2 ATR)")

style = {
    ChangeSignal.BUY: ("^", "#1b5e20", "BUY", -1),
    ChangeSignal.SELL: ("v", "#b71c1c", "SELL", 1),
    ChangeSignal.EXIT_FROM_BUY: ("x", "#33691e", "EXIT_FROM_BUY", 1),
    ChangeSignal.EXIT_FROM_SELL: ("x", "#880e4f", "EXIT_FROM_SELL", -1),
}
seen = set()
for t, px, sig in signals:
    m, col, lab, side = style[sig]
    y = px * (1 + 0.07 * side)
    ax.scatter(t, y, marker=m, s=110, color=col, zorder=5,
               label=lab if lab not in seen else None, linewidths=2.2)
    seen.add(lab)

ax.set_title("Weekly Triple-EMA + ATR regime — produced by trip_ema's own indicator code "
             "(synthetic data)", fontsize=12, loc="left")
ax.set_ylabel("Price")
ax.grid(alpha=0.25)
ax.margins(x=0.01)
ax.legend(loc="upper left", fontsize=8.5, ncol=2, framealpha=0.92)
txt = ("Background: green = BULLISH, red = BEARISH, none = NEUTRAL\n"
       "Signals fire on regime transitions (DetermineSignal)")
ax.text(0.995, 0.02, txt, transform=ax.transAxes, ha="right", va="bottom", fontsize=8.5,
        bbox=dict(boxstyle="round", fc="white", ec="#bbb"))
fig.tight_layout()
fig.savefig(out_path)
print(f"bars={n} signals={len(signals)} ->", out_path)
for t, px, sig in signals:
    print(t.date(), sig.name, round(px, 2))
