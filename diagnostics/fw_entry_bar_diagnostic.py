#!/usr/bin/env python3
"""
fw_entry_bar_diagnostic.py -- replicate the forward walk with a configurable entry bar.

Read-only. Writes a CSV to disk. Touches no database tables.

WHAT THIS IS
------------
`trip_ema.domain.forward_walk.walk_signal` enters at the open of the first daily
bar strictly after signal_date ("bar 1"). This program reproduces that geometry
exactly, but lets you shift the entry to bar 2, 3, ... N.

At --entry-bar 2 the "no exit/reversal on bar 1" condition is automatic, not a
separate filter: when the next signal lands on bar 1's trading date the window
collapses to exit_idx == entry_idx, so start_idx = entry_idx + 1 exceeds it and
the signal is skipped. Those are precisely the one-bar trades.

VERIFY BEFORE YOU TRUST IT
--------------------------
    python fw_entry_bar_diagnostic.py --entry-bar 1 --validate

reruns the walk at the production entry bar and diffs every row against
signal_labels. If that reports zero mismatches, the bar-2 numbers are computed
by the same code path and are comparable.

    python fw_entry_bar_diagnostic.py --entry-bar 2

NOT REPLICATED
--------------
excess_ret_industry / excess_ret_market. Those need the NIFTYBEES series and
industry_index_value with asof-backward lookup; none of the (a)/(b)/(c)
analyses use them. Hook point is marked WALK CONTEXT below.
"""

from __future__ import annotations

import argparse
import bisect
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

DEFAULT_URL = "postgresql+psycopg2://trip_gui_user@/trip_ema"

LONG, SHORT = "LONG", "SHORT"
NEXT_SIGNAL, DATA_END = "NEXT_SIGNAL", "DATA_END"
_WALK_SIDES = {"BUY": LONG, "SELL": SHORT}
_TOL = 1e-9

GAIN_RUNGS: list[tuple[str, float]] = [
    (str(n), n / 100.0)
    for n in (1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31, 33,
              35, 50, 75, 100)
]
LOSS_RUNGS: list[tuple[str, float]] = [
    ("5", 0.05), ("7_and_half", 0.075), ("10", 0.10),
    ("12_and_half", 0.125), ("15", 0.15), ("20", 0.20),
]


def _reached_up(x: float, level: float) -> bool:
    return x >= level - level * _TOL


def _reached_down(x: float, level: float) -> bool:
    return x <= level + level * _TOL


@dataclass(slots=True)
class Walk:
    """Price-derived geometry for one window. Mirrors ForwardWalkResult."""
    side: str
    entry_open: float
    exit_close: float
    bars_in_window: int
    realized_ret: float
    ever_in_profit: bool
    mae_pct: float
    mae_bar: int
    mfe_pct: float
    mfe_bar: int
    mae_before_mfe: bool
    mfe_atr20: float | None
    mae_atr20: float | None
    time_underwater_bars: int
    slope_per_bar: float | None
    realized_vol: float | None
    sharpe_in_window: float | None
    capture_ratio: float | None
    bars_to_gain: dict[str, int]
    bars_to_loss: dict[str, int]


def _ols_slope(ys) -> float | None:
    m = len(ys)
    if m < 2:
        return None
    mean_x = (m - 1) / 2.0
    mean_y = sum(ys) / m
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(ys))
    den = sum((i - mean_x) ** 2 for i in range(m))
    return None if den == 0 else num / den


def walk_signal(
    entry_open: float,
    o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,
    side: str,
    atr20: float | None,
) -> Walk:
    """Verbatim port of trip_ema.domain.forward_walk.walk_signal (price part)."""
    if side not in (LONG, SHORT):
        raise ValueError(f"side must be LONG or SHORT, got {side!r}")
    if entry_open is None or entry_open <= 0:
        raise ValueError(f"entry_open must be > 0, got {entry_open!r}")
    if len(c) == 0:
        raise ValueError("walk_signal requires at least the entry bar")

    is_long = side == LONG
    m = len(c)
    exit_close = float(c[-1])
    if (c <= 0).any():
        raise ValueError("walk_signal requires positive closes")

    realized_ret = (
        exit_close / entry_open - 1.0
        if is_long
        else (entry_open - exit_close) / entry_open
    )

    def _first_touch(frac: float, gain: bool) -> int:
        if is_long:
            if gain:
                level = entry_open * (1.0 + frac)
                idx = np.flatnonzero(h >= level - level * _TOL)
            else:
                level = entry_open * (1.0 - frac)
                idx = np.flatnonzero(l <= level + level * _TOL)
        else:
            if gain:
                level = entry_open * (1.0 - frac)
                idx = np.flatnonzero(l <= level + level * _TOL)
            else:
                level = entry_open * (1.0 + frac)
                idx = np.flatnonzero(h >= level - level * _TOL)
        return int(idx[0]) if idx.size else -1

    bars_to_gain = {lab: _first_touch(f, True) for lab, f in GAIN_RUNGS}
    bars_to_loss = {lab: _first_touch(f, False) for lab, f in LOSS_RUNGS}

    if is_long:
        favorable = (h - entry_open) / entry_open
        adverse = (entry_open - l) / entry_open
    else:
        favorable = (entry_open - l) / entry_open
        adverse = (h - entry_open) / entry_open

    mfe_pct = float(favorable.max())
    mfe_bar = int(favorable.argmax())      # argmax == list.index(max): first occurrence
    mae_pct = float(adverse.max())
    mae_bar = int(adverse.argmax())
    ever_in_profit = mfe_pct > 0.0
    mae_before_mfe = mae_bar < mfe_bar

    if atr20 not in (None, 0) and not (isinstance(atr20, float) and np.isnan(atr20)):
        mfe_atr20 = mfe_pct * entry_open / atr20
        mae_atr20 = mae_pct * entry_open / atr20
    else:
        mfe_atr20 = mae_atr20 = None

    time_underwater_bars = int((c < entry_open).sum() if is_long else (c > entry_open).sum())

    closes = [float(v) for v in c]
    slope = _ols_slope(closes)
    slope_per_bar = None if slope is None else slope / entry_open

    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, m)]
    if len(rets) >= 2:
        realized_vol = statistics.stdev(rets)
        sharpe_in_window = statistics.fmean(rets) / realized_vol if realized_vol > 0 else None
    else:
        realized_vol = sharpe_in_window = None

    capture_ratio = realized_ret / mfe_pct if mfe_pct > 0 else None

    return Walk(
        side=side, entry_open=entry_open, exit_close=exit_close, bars_in_window=m,
        realized_ret=realized_ret, ever_in_profit=ever_in_profit,
        mae_pct=mae_pct, mae_bar=mae_bar, mfe_pct=mfe_pct, mfe_bar=mfe_bar,
        mae_before_mfe=mae_before_mfe, mfe_atr20=mfe_atr20, mae_atr20=mae_atr20,
        time_underwater_bars=time_underwater_bars, slope_per_bar=slope_per_bar,
        realized_vol=realized_vol, sharpe_in_window=sharpe_in_window,
        capture_ratio=capture_ratio, bars_to_gain=bars_to_gain, bars_to_loss=bars_to_loss,
    )


SQL_SIGNALS = text("""
SELECT e.signal_enrichment_id, e.ticker_id, e.timeframe, e.signal_date,
       e.signal_type, e.atr20
  FROM signal_enrichment e
  JOIN tickers k ON k.ticker_id = e.ticker_id
 WHERE (:eq_only = false OR k.instrument_type = 'EQ')
 ORDER BY e.ticker_id, e.timeframe, e.signal_date, e.signal_enrichment_id
""")

SQL_BARS = text("""
SELECT o.ticker_id,
       (o.bar_timestamp AT TIME ZONE :zone)::date AS d,
       o.open, o.high, o.low, o.close
  FROM ohlcv_bars o
  JOIN tickers k ON k.ticker_id = o.ticker_id
 WHERE o.timeframe = '1D'
   AND (:eq_only = false OR k.instrument_type = 'EQ')
 ORDER BY o.ticker_id, o.bar_timestamp
""")

SQL_CENSOR = text("""
SELECT ticker_id, ex_date
  FROM nse_corporate_actions
 WHERE censor = true AND ticker_id IS NOT NULL
""")

SQL_LABELS = text("""
SELECT signal_enrichment_id, side, entry_date, exit_date, bars_in_window,
       realized_ret, mfe_pct, mae_pct, mfe_bar, mae_bar,
       ever_in_profit, mae_before_mfe, window_censored
  FROM signal_labels
 WHERE forward_walk_done
""")


def run_walks(sig_df, bars_df, censor, entry_bar: int) -> pd.DataFrame:
    bars_by_ticker: dict[int, tuple] = {}
    for tid, grp in bars_df.groupby("ticker_id", sort=False):
        bars_by_ticker[tid] = (
            grp["d"].tolist(),
            grp["open"].to_numpy(dtype=float),
            grp["high"].to_numpy(dtype=float),
            grp["low"].to_numpy(dtype=float),
            grp["close"].to_numpy(dtype=float),
        )

    rows: list[dict] = []
    skipped = {"no_bars": 0, "past_data_end": 0, "degenerate": 0,
               "no_room_for_entry": 0, "bad_prices": 0}

    for (tid, _tf), sigs in sig_df.groupby(["ticker_id", "timeframe"], sort=False):
        pack = bars_by_ticker.get(tid)
        if pack is None:
            skipped["no_bars"] += len(sigs)
            continue
        bar_dates, o, h, l, c = pack
        last_bar_date = bar_dates[-1]
        cen = censor.get(tid, [])

        recs = sigs.to_dict("records")
        n = len(recs)
        for i, sig in enumerate(recs):
            side = _WALK_SIDES.get(sig["signal_type"])
            if side is None:
                continue

            entry_idx = bisect.bisect_right(bar_dates, sig["signal_date"])
            if entry_idx >= len(bar_dates):
                skipped["past_data_end"] += 1
                continue

            if i + 1 < n:
                n_date = recs[i + 1]["signal_date"]
                exit_reason, right_censored = NEXT_SIGNAL, False
            else:
                n_date = last_bar_date
                exit_reason, right_censored = DATA_END, True

            exit_idx = bisect.bisect_right(bar_dates, n_date) - 1
            if exit_idx < entry_idx:
                skipped["degenerate"] += 1
                continue

            # ---- the only departure from production ----------------------
            start_idx = entry_idx + (entry_bar - 1)
            if start_idx > exit_idx:
                # No room to enter. At entry_bar=2 this is exactly the
                # "signal reversed/exited on bar 1" population.
                skipped["no_room_for_entry"] += 1
                continue
            # --------------------------------------------------------------

            entry_date, exit_date = bar_dates[start_idx], bar_dates[exit_idx]
            is_cen = any(entry_date <= d <= exit_date for d in cen)

            base = {
                "signal_enrichment_id": sig["signal_enrichment_id"],
                "ticker_id": tid,
                "signal_date": sig["signal_date"],
                "side": side,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "exit_reason": exit_reason,
                "bars_in_window": exit_idx - start_idx + 1,
                "window_censored": bool(is_cen or right_censored),
            }
            if is_cen:
                rows.append(base)
                continue

            sl = slice(start_idx, exit_idx + 1)
            try:
                # WALK CONTEXT: industry_ret / market_ret would be supplied here.
                w = walk_signal(float(o[start_idx]), o[sl], h[sl], l[sl], c[sl],
                                side, sig["atr20"])
            except ValueError:
                skipped["bad_prices"] += 1
                continue

            base.update({
                "entry_open": w.entry_open, "exit_close": w.exit_close,
                "realized_ret": w.realized_ret, "ever_in_profit": w.ever_in_profit,
                "mae_pct": w.mae_pct, "mae_bar": w.mae_bar,
                "mfe_pct": w.mfe_pct, "mfe_bar": w.mfe_bar,
                "mae_before_mfe": w.mae_before_mfe,
                "mfe_atr20": w.mfe_atr20, "mae_atr20": w.mae_atr20,
                "time_underwater_bars": w.time_underwater_bars,
                "slope_per_bar": w.slope_per_bar, "realized_vol": w.realized_vol,
                "sharpe_in_window": w.sharpe_in_window, "capture_ratio": w.capture_ratio,
            })
            for lab, v in w.bars_to_gain.items():
                base[f"bars_to_gain_{lab}_pct"] = v
                base[f"hit_gain_{lab}_pct"] = v != -1
            for lab, v in w.bars_to_loss.items():
                base[f"bars_to_loss_{lab}_pct"] = v
                base[f"hit_loss_{lab}_pct"] = v != -1
            rows.append(base)

    print(f"  skipped: {skipped}", file=sys.stderr)
    return pd.DataFrame(rows)


def validate(df: pd.DataFrame, engine) -> int:
    with engine.connect() as conn:
        db = pd.read_sql(SQL_LABELS, conn)
    m = df.merge(db, on="signal_enrichment_id", how="outer",
                 suffixes=("_new", "_db"), indicator=True)

    print("\n=== VALIDATION vs signal_labels ===")
    print(f"  in both      : {(m['_merge'] == 'both').sum():,}")
    print(f"  computed only: {(m['_merge'] == 'left_only').sum():,}")
    print(f"  db only      : {(m['_merge'] == 'right_only').sum():,}")

    b = m[m["_merge"] == "both"]
    bad = 0
    for col, tol in [("realized_ret", 1e-9), ("mfe_pct", 1e-9), ("mae_pct", 1e-9)]:
        x, y = b[f"{col}_new"], b[f"{col}_db"]
        both_null = x.isna() & y.isna()
        d = (~both_null) & ~(np.isclose(x, y, rtol=tol, atol=tol, equal_nan=True))
        print(f"  {col:<22} mismatched: {int(d.sum()):,}")
        bad += int(d.sum())
    for col in ["bars_in_window", "mfe_bar", "mae_bar", "ever_in_profit",
                "mae_before_mfe", "side", "entry_date", "exit_date", "window_censored"]:
        d = b[f"{col}_new"].astype(str) != b[f"{col}_db"].astype(str)
        print(f"  {col:<22} mismatched: {int(d.sum()):,}")
        bad += int(d.sum())

    extra = int((m["_merge"] != "both").sum())
    print(f"\n  TOTAL discrepancies: {bad + extra:,}")
    if bad + extra == 0:
        print("  CLEAN -- geometry reproduces production exactly.")
    return bad + extra


def analyse(df: pd.DataFrame) -> None:
    d = df[(~df["window_censored"].astype(bool)) & df["realized_ret"].notna()].copy()
    print(f"\n(analysis set: {len(d):,} rows, censored + DATA_END excluded)")

    print("\n=== (a) ever_in_profit by side ===")
    print(f"{'side':<7}{'n':>9}{'n_ever_profit':>15}{'pct_ever_profit':>17}{'pct_end_profit':>16}")
    for side, g in d.groupby("side"):
        print(f"{side:<7}{len(g):>9,}{int(g['ever_in_profit'].sum()):>15,}"
              f"{100 * g['ever_in_profit'].mean():>17.1f}"
              f"{100 * (g['realized_ret'] > 0).mean():>16.1f}")

    print("\n=== (b) ever_in_profit x mae_before_mfe ===")
    hdr = (f"{'side':<7}{'ever_prof':>11}{'mae_b4_mfe':>12}{'n':>9}"
           f"{'pct_side':>10}{'win_pct':>9}{'mean_pct':>10}{'med_pct':>9}")
    print(hdr)
    print("-" * len(hdr))
    for side, g in d.groupby("side"):
        for (ep, mb), cell in g.groupby(["ever_in_profit", "mae_before_mfe"]):
            print(f"{side:<7}{str(ep):>11}{str(mb):>12}{len(cell):>9,}"
                  f"{100 * len(cell) / len(g):>10.1f}"
                  f"{100 * (cell['realized_ret'] > 0).mean():>9.1f}"
                  f"{100 * cell['realized_ret'].mean():>10.2f}"
                  f"{100 * cell['realized_ret'].median():>9.2f}")

    print("\n=== (c) mfe_pct frequency distribution ===")
    mp = d["mfe_pct"] * 100
    d["bk"] = np.where(mp <= 0, 0, np.where(mp > 30, 16, np.ceil(mp / 2.0))).astype(int)

    def lbl(b: int) -> str:
        if b == 0:
            return "0%"
        if b == 16:
            return "30%+"
        return f"{(b - 1) * 2:>2}-{b * 2}%"

    hdr = (f"{'side':<7}{'mfe_bucket':>12}{'n':>9}{'pct':>8}{'cum_pct':>10}"
           f"{'win_pct':>9}{'mean_ret_pct':>14}")
    print(hdr)
    print("-" * len(hdr))
    for side, g in d.groupby("side"):
        tot, cum = len(g), 0
        for b, cell in g.groupby("bk"):
            cum += len(cell)
            print(f"{side:<7}{lbl(b):>12}{len(cell):>9,}{100 * len(cell) / tot:>8.2f}"
                  f"{100 * cum / tot:>10.2f}"
                  f"{100 * (cell['realized_ret'] > 0).mean():>9.1f}"
                  f"{100 * cell['realized_ret'].mean():>14.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entry-bar", type=int, default=2,
                    help="1 = production behaviour, 2 = deferred entry (default 2)")
    ap.add_argument("--validate", action="store_true",
                    help="diff against signal_labels (only meaningful with --entry-bar 1)")
    ap.add_argument("--zone", default="Asia/Kolkata")
    ap.add_argument("--all-instruments", action="store_true",
                    help="include ETFs etc; default is instrument_type='EQ' only")
    ap.add_argument("--out", default=None)
    ap.add_argument("--url", default=os.environ.get("TRIP_DB_URL", DEFAULT_URL))
    args = ap.parse_args()

    if args.entry_bar < 1:
        print("--entry-bar must be >= 1", file=sys.stderr)
        return 2

    eq_only = not args.all_instruments
    out = Path(args.out or f"diagnostics/fw_entry_bar{args.entry_bar}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"entry_bar={args.entry_bar}  eq_only={eq_only}  zone={args.zone}")
    engine = create_engine(args.url)
    with engine.connect() as conn:
        print("loading signals...", file=sys.stderr)
        sig_df = pd.read_sql(SQL_SIGNALS, conn, params={"eq_only": eq_only})
        print(f"  {len(sig_df):,} signals", file=sys.stderr)
        print("loading bars...", file=sys.stderr)
        bars_df = pd.read_sql(SQL_BARS, conn,
                              params={"zone": args.zone, "eq_only": eq_only})
        print(f"  {len(bars_df):,} bars", file=sys.stderr)
        cen_df = pd.read_sql(SQL_CENSOR, conn)

    censor: dict[int, list[date]] = {}
    for tid, grp in cen_df.groupby("ticker_id"):
        censor[int(tid)] = grp["ex_date"].tolist()

    print("walking...", file=sys.stderr)
    df = run_walks(sig_df, bars_df, censor, args.entry_bar)
    if df.empty:
        print("no walks produced", file=sys.stderr)
        return 1
    print(f"  {len(df):,} walk rows")

    df.to_csv(out, index=False)
    print(f"written: {out}")

    rc = 0
    if args.validate:
        if args.entry_bar != 1:
            print("\nWARNING: --validate at entry-bar != 1 will mismatch by design.")
        rc = 1 if validate(df, engine) else 0

    analyse(df)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
