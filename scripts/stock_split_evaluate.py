"""Decisive test: does yfinance auto_adjust=True split-adjust NSE prices?

Question
--------
Our stored `adj_close` (fetched with auto_adjust=False → Yahoo's "Adj Close"
field) is dividend-only for NSE: at a 1:2 split it halves alongside raw close
instead of staying flat. The open question is whether auto_adjust=True is:
  (A) element-wise equal to that same Adj Close field  -> still split-broken
      (my claim: it just re-serves Yahoo's adjclose), OR
  (B) a genuinely split-adjusted series that yfinance recomputes from split
      events -> flipping the flag would FIX splits (your reading).

This settles it three ways at once, per stock:
  1. Is auto_adjust=True's Close element-wise EQUAL to auto_adjust=False's
     "Adj Close"? (max relative diff)
  2. At each real split date, what is the overnight ratio in each series?
       - raw Close should JUMP (ratio ~= 1/split_factor)
       - a split-adjusted series should be ~FLAT (ratio ~= 1)
       - whichever of adjclose / auto_adjust is flat is the split-adjusted one
  3. Verdict per stock, then overall.

RUN (on the NUC, venv active):
    python aa_split_test.py INFY.NS WIPRO.NS RELIANCE.NS
  or pass your own known splitters (ideally the symbols behind the ticker_ids
  from the split dump, e.g. ticker 1670's symbol):
    python aa_split_test.py <SYM1>.NS <SYM2>.NS <SYM3>.NS
"""
from __future__ import annotations

import sys

START = "2006-01-01"
_DEFAULTS = ["INFY.NS", "WIPRO.NS", "RELIANCE.NS"]  # all have bonuses/splits since 2006

# thresholds
_IDENTICAL_REL = 0.005   # aa vs adjclose considered "same series" below this
_FLAT_LO, _FLAT_HI = 0.90, 1.11   # "absorbed the split" band for an overnight ratio


def _ratio_at(series, idx, split_date):
    """Overnight ratio close(D)/close(D-1) for the given split ex-date."""
    try:
        pos = idx.get_loc(split_date)
    except KeyError:
        return None
    if isinstance(pos, slice) or pos == 0:
        return None
    cur = float(series.iloc[pos])
    prev = float(series.iloc[pos - 1])
    if prev == 0:
        return None
    return cur / prev


def compare(sym, raw_df, aa_df):
    """Pure analysis. raw_df: auto_adjust=False frame (has 'Adj Close' +
    'Stock Splits'). aa_df: auto_adjust=True frame ('Close' is adjusted)."""
    import pandas as pd  # noqa: F401 — only for isna, imported lazily

    # align on the common date index
    idx = raw_df.index.intersection(aa_df.index)
    if len(idx) == 0:
        print(f"\n### {sym}: no overlapping dates — SKIP")
        return
    raw = raw_df.loc[idx]
    aa = aa_df.loc[idx]

    raw_close = raw["Close"]
    adjclose = raw["Adj Close"]
    aa_close = aa["Close"]
    splits = raw["Stock Splits"] if "Stock Splits" in raw.columns else None

    print(f"\n{'=' * 70}\n### {sym}   ({len(idx)} aligned rows, "
          f"{idx[0].date()} → {idx[-1].date()})\n{'=' * 70}")

    # ---- (1) is auto_adjust=True Close the same series as Adj Close? ----
    denom = adjclose.replace(0, float("nan")).abs()
    rel = ((aa_close - adjclose).abs() / denom).max()
    same = rel < _IDENTICAL_REL
    print(f"[1] max |auto_adjust=True.Close - adj_close| / adj_close = {rel:.6f}"
          f"   ->  {'IDENTICAL SERIES' if same else 'DIFFERENT SERIES'}")

    # oldest-row absolute values, to see the cumulative divergence
    print(f"    oldest row {idx[0].date()}:  raw_close={raw_close.iloc[0]:.4f}  "
          f"adj_close={adjclose.iloc[0]:.4f}  aa_close={aa_close.iloc[0]:.4f}")

    # ---- (2) behaviour at real split dates ----
    split_dates = []
    if splits is not None:
        split_dates = [d for d in idx[splits != 0]]
    if not split_dates:
        print("[2] Ticker reports NO split events in window "
              "(actions feed empty — see note below).")
    else:
        print(f"[2] {len(split_dates)} split event(s) reported by yfinance. "
              f"Overnight ratios (flat≈absorbed, jump≈not):")
        print(f"    {'ex_date':<12}{'split_f':>8}{'raw':>9}{'adjclose':>10}{'auto_adj':>10}")
        for d in split_dates:
            sf = float(splits.loc[d])
            rr = _ratio_at(raw_close, idx, d)
            ar = _ratio_at(adjclose, idx, d)
            ur = _ratio_at(aa_close, idx, d)
            fmt = lambda x: f"{x:>9.4f}" if x is not None else f"{'—':>9}"
            print(f"    {str(d.date()):<12}{sf:>8.3f}"
                  f"{fmt(rr)}{fmt(ar):>10}{fmt(ur):>10}")

    # ---- (3) per-stock verdict ----
    verdict = _verdict(same, split_dates, raw_close, adjclose, aa_close, splits, idx)
    print(f"[3] VERDICT: {verdict}")
    return verdict


def _verdict(same, split_dates, raw_close, adjclose, aa_close, splits, idx):
    if not split_dates:
        if same:
            return ("auto_adjust=True == stored adj_close, and no splits to "
                    "test here. Inconclusive on splits; identical on dividends.")
        return "differs from adj_close but no split in window — inconclusive."

    # classify split absorption for aa vs adjclose
    aa_absorbed = adj_absorbed = raw_jumped = 0
    for d in split_dates:
        ur = _ratio_at(aa_close, idx, d)
        ar = _ratio_at(adjclose, idx, d)
        rr = _ratio_at(raw_close, idx, d)
        if ur is not None and _FLAT_LO <= ur <= _FLAT_HI:
            aa_absorbed += 1
        if ar is not None and _FLAT_LO <= ar <= _FLAT_HI:
            adj_absorbed += 1
        if rr is not None and (rr < _FLAT_LO or rr > _FLAT_HI):
            raw_jumped += 1

    n = len(split_dates)
    if aa_absorbed >= max(1, n - 1) and adj_absorbed == 0:
        return (f"auto_adjust=True IS SPLIT-ADJUSTED ({aa_absorbed}/{n} absorbed) "
                f"while adj_close is NOT ({adj_absorbed}/{n}). Flipping the flag "
                f"FIXES splits — my earlier claim was WRONG for your data.")
    if same and aa_absorbed == 0:
        return (f"auto_adjust=True is element-wise equal to adj_close and neither "
                f"absorbs splits ({aa_absorbed}/{n}). Same broken series — the "
                f"flag flip changes nothing. (Split factor must be built from events.)")
    if aa_absorbed == 0 and adj_absorbed == 0:
        return (f"NEITHER series absorbs splits ({aa_absorbed}/{n}, {adj_absorbed}/{n}); "
                f"raw jumped at {raw_jumped}/{n}. Yahoo's adjustment is split-broken "
                f"regardless of the flag. Build the factor ourselves.")
    return (f"MIXED: aa absorbed {aa_absorbed}/{n}, adj absorbed {adj_absorbed}/{n}. "
            f"Inspect the table above by hand.")


def main():
    symbols = sys.argv[1:] or _DEFAULTS
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance not importable in this env.", file=sys.stderr)
        return 2

    print(f"yfinance {getattr(yf, '__version__', '?')} | symbols: {symbols} | from {START}")
    print("NOTE: if [2] shows NO split events, Yahoo's actions feed lacks them for "
          "these tickers — which is itself the likely reason its adjclose is "
          "split-broken. In that case column [2] can't test absorption and we must "
          "detect splits from raw-price ratios (in-DB), not from Yahoo events.")

    overall = []
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            raw_df = t.history(start=START, auto_adjust=False, actions=True)
            aa_df = t.history(start=START, auto_adjust=True, actions=True)
            if raw_df.empty or aa_df.empty:
                print(f"\n### {sym}: empty frame(s) — SKIP")
                continue
            overall.append((sym, compare(sym, raw_df, aa_df)))
        except Exception as e:  # noqa: BLE001
            print(f"\n### {sym}: fetch/compare error: {type(e).__name__}: {e}")

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for sym, v in overall:
        print(f"  {sym}: {v}")
    return 0


if __name__ == "__main__":
