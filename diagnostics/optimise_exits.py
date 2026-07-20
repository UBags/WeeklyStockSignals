#!/usr/bin/env python3
"""
optimise_exits.py -- conditional exit analysis for the Triple-EMA book.

Read-only. Writes CSVs. Touches no database tables.

Shares the window geometry of diagnose_profits_at_n_bars.py: entry at the OPEN
of bar N after signal_date, exit at the CLOSE of the window-end bar, with the
N>1 regime guard (no signal of any type may fire on bars 1..N-1).

WHAT IT ANSWERS
---------------
1. STOP TABLE -- for each loss level x:
       P(the trade EVER trades back above entry, after first touching -x%)
   Read off the x where this falls below 20% / 10% / 5% / 1%. Below 1% the
   stop is nearly free: you are only cutting trades that never came back.

2. TARGET TABLE -- for each gain level a:
       P(the trade runs HIGHER than +a%, after first touching +a%)
   Same thresholds. This is the take-profit question.

   Both are measured on bars STRICTLY AFTER the touch bar, because intrabar
   ordering is unknowable: if you stop out at -x% mid-bar you cannot claim the
   rest of that bar.

3. GRID -- exact simulation over (stop_loss, take_profit) pairs. Walks
   first-touch indices; whichever triggers first wins; ties go to the STOP
   (conservative). Reports per-trade mean/median and return on capital-time,
   plus a per-year sign test against the no-stop/no-target baseline.

FILL ASSUMPTIONS (both optimistic -- read the results as an upper bound)
    * a stop fills exactly at -x%, no gap-through
    * a target fills exactly at +a%
    * same-bar stop+target resolves to the stop

USAGE
    python optimise_exits.py --n 3 --side LONG
    python optimise_exits.py --n 1 --side LONG --all-instruments
"""

from __future__ import annotations

import argparse
import bisect
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

DEFAULT_URL = "postgresql+psycopg2://trip_gui_user@/trip_ema"
LONG, SHORT = "LONG", "SHORT"
_WALK_SIDES = {"BUY": LONG, "SELL": SHORT}
_TOL = 1e-9

# grids in percent
LOSS_GRID = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 18, 20,
                      25, 30, 35, 40, 50], dtype=float)
GAIN_GRID = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 18, 20,
                      25, 30, 35, 40, 50, 75, 100], dtype=float)
SL_GRID = [None, 5.0, 7.5, 10.0, 12.5, 15.0, 20.0, 25.0, 30.0, 40.0]
TP_GRID = [None, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 75.0, 100.0]

SQL_SIGNALS = text("""
SELECT e.signal_enrichment_id, e.ticker_id, e.timeframe, e.signal_date,
       e.signal_type
  FROM signal_enrichment e
  JOIN tickers k ON k.ticker_id = e.ticker_id
 WHERE (:eq_only = false OR k.instrument_type = 'EQ')
 ORDER BY e.ticker_id, e.timeframe, e.signal_date, e.signal_enrichment_id
""")

SQL_BARS = text("""
SELECT o.ticker_id, (o.bar_timestamp AT TIME ZONE :zone)::date AS d,
       o.open, o.high, o.low, o.close
  FROM ohlcv_bars o
  JOIN tickers k ON k.ticker_id = o.ticker_id
 WHERE o.timeframe = '1D'
   AND (:eq_only = false OR k.instrument_type = 'EQ')
 ORDER BY o.ticker_id, o.bar_timestamp
""")

SQL_CENSOR = text("""
SELECT ticker_id, ex_date FROM nse_corporate_actions
 WHERE censor = true AND ticker_id IS NOT NULL
""")


def _first_touch_up(x: np.ndarray, level: float) -> int:
    idx = np.flatnonzero(x >= level - abs(level) * _TOL)
    return int(idx[0]) if idx.size else -1


def _first_touch_dn(x: np.ndarray, level: float) -> int:
    idx = np.flatnonzero(x <= level + abs(level) * _TOL)
    return int(idx[0]) if idx.size else -1


def per_trade(entry: float, h: np.ndarray, l: np.ndarray, c: np.ndarray,
              is_long: bool) -> dict:
    """First-touch indices for both grids + post-touch recovery/continuation."""
    m = len(c)
    exit_close = float(c[-1])
    realized = (exit_close / entry - 1.0) if is_long else (entry - exit_close) / entry

    # favourable extreme per bar, and adverse extreme per bar
    fav = h if is_long else l          # LONG gains on highs, SHORT on lows
    adv = l if is_long else h

    # suffix extremes: best favourable price from bar i onward
    if is_long:
        suf_fav = np.maximum.accumulate(fav[::-1])[::-1]      # max high
    else:
        suf_fav = np.minimum.accumulate(fav[::-1])[::-1]      # min low

    loss_bar = np.full(LOSS_GRID.size, -1, dtype=np.int32)
    loss_recov = np.zeros(LOSS_GRID.size, dtype=bool)   # ever back above entry after
    loss_final = np.zeros(LOSS_GRID.size, dtype=bool)   # finished profitable
    for k, x in enumerate(LOSS_GRID):
        lvl = entry * (1.0 - x / 100.0) if is_long else entry * (1.0 + x / 100.0)
        i = _first_touch_dn(adv, lvl) if is_long else _first_touch_up(adv, lvl)
        loss_bar[k] = i
        if i < 0:
            continue
        if i + 1 < m:
            best = suf_fav[i + 1]
            loss_recov[k] = (best > entry) if is_long else (best < entry)
        loss_final[k] = realized > 0

    gain_bar = np.full(GAIN_GRID.size, -1, dtype=np.int32)
    gain_further = np.zeros(GAIN_GRID.size, dtype=bool)   # ran beyond +a% later
    gain_final = np.zeros(GAIN_GRID.size, dtype=bool)
    gain_best_after = np.full(GAIN_GRID.size, np.nan)     # best % reached from i+1 on
    for k, a in enumerate(GAIN_GRID):
        lvl = entry * (1.0 + a / 100.0) if is_long else entry * (1.0 - a / 100.0)
        j = _first_touch_up(fav, lvl) if is_long else _first_touch_dn(fav, lvl)
        gain_bar[k] = j
        if j < 0:
            continue
        if j + 1 < m:
            best = suf_fav[j + 1]
            gain_further[k] = (best > lvl) if is_long else (best < lvl)
            gain_best_after[k] = ((best / entry - 1.0) if is_long
                                  else (entry - best) / entry) * 100.0
        gain_final[k] = realized > 0

    return {
        "realized": realized, "bars": m,
        "loss_bar": loss_bar, "loss_recov": loss_recov, "loss_final": loss_final,
        "gain_bar": gain_bar, "gain_further": gain_further,
        "gain_final": gain_final, "gain_best_after": gain_best_after,
    }


def collect(sig_df, bars_df, censor, *, n: int, side_want: str,
            eq_only: bool) -> tuple[pd.DataFrame, dict]:
    packs = {}
    for tid, grp in bars_df.groupby("ticker_id", sort=False):
        packs[tid] = (grp["d"].tolist(),
                      grp["open"].to_numpy(float), grp["high"].to_numpy(float),
                      grp["low"].to_numpy(float), grp["close"].to_numpy(float))

    rows, LB, LR, LF, GB, GF, GN, GA = [], [], [], [], [], [], [], []
    skipped = {"regime_change_before_entry": 0, "entry_beyond_data": 0,
               "past_data_end": 0, "window_ends_before_entry": 0,
               "censored": 0, "bad_prices": 0, "wrong_side": 0}

    for (tid, _tf), sigs in sig_df.groupby(["ticker_id", "timeframe"], sort=False):
        pack = packs.get(tid)
        if pack is None:
            continue
        bar_dates, o, h, l, c = pack
        nbars = len(bar_dates)
        cen = censor.get(tid, [])
        recs = sigs.to_dict("records")
        total = len(recs)

        for i, sig in enumerate(recs):
            side = _WALK_SIDES.get(sig["signal_type"])
            if side is None:
                continue
            if side != side_want:
                skipped["wrong_side"] += 1
                continue

            entry_idx = bisect.bisect_right(bar_dates, sig["signal_date"])
            if entry_idx >= nbars:
                skipped["past_data_end"] += 1
                continue
            start_idx = entry_idx + (n - 1)
            if start_idx >= nbars:
                skipped["entry_beyond_data"] += 1
                continue
            if n > 1 and i + 1 < total and \
                    recs[i + 1]["signal_date"] <= bar_dates[start_idx - 1]:
                skipped["regime_change_before_entry"] += 1
                continue

            if i + 1 < total:
                n_date = recs[i + 1]["signal_date"]
                right_censored = False
            else:
                n_date = bar_dates[-1]
                right_censored = True
            exit_idx = bisect.bisect_right(bar_dates, n_date) - 1
            if exit_idx < start_idx:
                skipped["window_ends_before_entry"] += 1
                continue

            e_date, x_date = bar_dates[start_idx], bar_dates[exit_idx]
            if right_censored or any(e_date <= d <= x_date for d in cen):
                skipped["censored"] += 1
                continue

            sl = slice(start_idx, exit_idx + 1)
            entry = float(o[start_idx])
            hh, ll, cc = h[sl], l[sl], c[sl]
            if entry <= 0 or (cc <= 0).any():
                skipped["bad_prices"] += 1
                continue

            r = per_trade(entry, hh, ll, cc, side == LONG)
            rows.append({"yr": e_date.year, "realized": r["realized"],
                         "bars": r["bars"]})
            LB.append(r["loss_bar"]); LR.append(r["loss_recov"])
            LF.append(r["loss_final"]); GB.append(r["gain_bar"])
            GF.append(r["gain_further"]); GN.append(r["gain_final"])
            GA.append(r["gain_best_after"])

    df = pd.DataFrame(rows)
    arrs = {
        "loss_bar": np.array(LB), "loss_recov": np.array(LR),
        "loss_final": np.array(LF), "gain_bar": np.array(GB),
        "gain_further": np.array(GF), "gain_final": np.array(GN),
        "gain_best_after": np.array(GA),
    }
    print("  skipped:", {k: v for k, v in skipped.items() if v}, file=sys.stderr)
    return df, arrs


def _threshold_line(grid, prob, targets=(20.0, 10.0, 5.0, 1.0)) -> str:
    out = []
    for t in targets:
        hit = np.flatnonzero(prob < t)
        out.append(f"<{t:g}% at {grid[hit[0]]:g}%" if hit.size else f"<{t:g}% never")
    return "   ".join(out)


def stop_table(df, A, side) -> None:
    n = len(df)
    lb, lr, lf = A["loss_bar"], A["loss_recov"], A["loss_final"]
    ret = df["realized"].to_numpy() * 100

    print(f"\n=== STOP TABLE ({side}) -- given the trade first touched -x% ===")
    hdr = (f"{'x%':>6}{'n_hit':>9}{'%book':>8}{'P(recover)%':>13}"
           f"{'P(final>0)%':>13}{'mean_final%':>13}{'mean_if_stopped%':>18}")
    print(hdr); print("-" * len(hdr))
    probs = []
    for k, x in enumerate(LOSS_GRID):
        hit = lb[:, k] >= 0
        nh = int(hit.sum())
        if nh == 0:
            probs.append(100.0); continue
        p_rec = 100.0 * lr[hit, k].mean()
        p_fin = 100.0 * lf[hit, k].mean()
        mean_final = ret[hit].mean()
        # book mean if we had stopped every hit trade at -x%
        stopped = np.where(hit, -x, ret)
        probs.append(p_rec)
        print(f"{x:>6g}{nh:>9,}{100*nh/n:>8.1f}{p_rec:>13.2f}{p_fin:>13.2f}"
              f"{mean_final:>13.2f}{stopped.mean():>18.2f}")
    print(f"\n  P(recover) thresholds: {_threshold_line(LOSS_GRID, np.array(probs))}")
    print(f"  baseline book mean: {ret.mean():.2f}%")


def target_table(df, A, side) -> None:
    n = len(df)
    gb, gf, gn, ga = (A["gain_bar"], A["gain_further"], A["gain_final"],
                      A["gain_best_after"])
    ret = df["realized"].to_numpy() * 100

    print(f"\n=== TARGET TABLE ({side}) -- given the trade first touched +a% ===")
    hdr = (f"{'a%':>6}{'n_hit':>9}{'%book':>8}{'P(runs further)%':>18}"
           f"{'P(final>0)%':>13}{'mean_final%':>13}{'mean_if_taken%':>16}")
    print(hdr); print("-" * len(hdr))
    probs = []
    for k, a in enumerate(GAIN_GRID):
        hit = gb[:, k] >= 0
        nh = int(hit.sum())
        if nh == 0:
            probs.append(0.0); continue
        p_more = 100.0 * gf[hit, k].mean()
        p_fin = 100.0 * gn[hit, k].mean()
        taken = np.where(hit, a, ret)
        probs.append(p_more)
        print(f"{a:>6g}{nh:>9,}{100*nh/n:>8.1f}{p_more:>18.2f}{p_fin:>13.2f}"
              f"{ret[hit].mean():>13.2f}{taken.mean():>16.2f}")
    print(f"\n  P(runs further) thresholds: "
          f"{_threshold_line(GAIN_GRID, np.array(probs))}")
    print(f"  baseline book mean: {ret.mean():.2f}%")


def grid_sim(df, A, side, out: Path) -> None:
    ret = df["realized"].to_numpy() * 100
    bars = df["bars"].to_numpy(float)
    yr = df["yr"].to_numpy()
    lb, gb = A["loss_bar"], A["gain_bar"]
    base_mean, base_ann = ret.mean(), ret.sum() / bars.sum() * 252
    years = np.unique(yr)
    base_yr = np.array([ret[yr == y].mean() for y in years])
    big = np.iinfo(np.int32).max

    print(f"\n=== (SL, TP) GRID ({side}) -- ties go to the STOP ===")
    hdr = (f"{'SL%':>7}{'TP%':>7}{'win%':>8}{'mean%':>9}{'med%':>9}"
           f"{'mean_bars':>11}{'ann%':>9}{'yrs>base':>10}")
    print(hdr); print("-" * len(hdr))
    recs = []
    for sl in SL_GRID:
        for tp in TP_GRID:
            si = (np.where(lb[:, np.searchsorted(LOSS_GRID, sl)] >= 0,
                           lb[:, np.searchsorted(LOSS_GRID, sl)], big)
                  if sl is not None and sl in LOSS_GRID else np.full(len(ret), big))
            ti = (np.where(gb[:, np.searchsorted(GAIN_GRID, tp)] >= 0,
                           gb[:, np.searchsorted(GAIN_GRID, tp)], big)
                  if tp is not None and tp in GAIN_GRID else np.full(len(ret), big))
            stop_first = si <= ti
            r = np.where((si < big) & stop_first, -float(sl or 0),
                         np.where(ti < big, float(tp or 0), ret))
            b = np.where((si < big) & stop_first, si + 1.0,
                         np.where(ti < big, ti + 1.0, bars))
            wins = sum(1 for i, y in enumerate(years)
                       if r[yr == y].mean() > base_yr[i])
            row = {"sl": sl, "tp": tp, "n": len(r),
                   "win_pct": 100 * (r > 0).mean(), "mean_pct": r.mean(),
                   "med_pct": float(np.median(r)), "mean_bars": b.mean(),
                   "ann_pct": r.sum() / b.sum() * 252,
                   "yrs_beat_base": wins, "yrs": len(years)}
            recs.append(row)
            print(f"{'-' if sl is None else sl:>7}{'-' if tp is None else tp:>7}"
                  f"{row['win_pct']:>8.1f}{row['mean_pct']:>9.2f}"
                  f"{row['med_pct']:>9.2f}{row['mean_bars']:>11.1f}"
                  f"{row['ann_pct']:>9.2f}{wins:>7}/{len(years)}")
    pd.DataFrame(recs).to_csv(out, index=False)
    print(f"\n  baseline: mean {base_mean:.2f}%  ann {base_ann:.2f}%  "
          f"over {len(years)} years")
    print(f"  grid written: {out}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--n", type=int, default=3)
    ap.add_argument("--side", choices=[LONG, SHORT], default=LONG)
    ap.add_argument("--zone", default="Asia/Kolkata")
    ap.add_argument("--all-instruments", action="store_true")
    ap.add_argument("--outdir", default="diagnostics")
    ap.add_argument("--url", default=os.environ.get("TRIP_DB_URL", DEFAULT_URL))
    args = ap.parse_args()

    eq_only = not args.all_instruments
    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    print(f"n={args.n}  side={args.side}  eq_only={eq_only}")

    engine = create_engine(args.url)
    with engine.connect() as conn:
        print("loading signals...", file=sys.stderr)
        sig_df = pd.read_sql(SQL_SIGNALS, conn, params={"eq_only": eq_only})
        print("loading bars...", file=sys.stderr)
        bars_df = pd.read_sql(SQL_BARS, conn,
                              params={"zone": args.zone, "eq_only": eq_only})
        cen_df = pd.read_sql(SQL_CENSOR, conn)
    censor: dict[int, list[date]] = {
        int(t): g["ex_date"].tolist() for t, g in cen_df.groupby("ticker_id")}

    print("walking...", file=sys.stderr)
    df, A = collect(sig_df, bars_df, censor, n=args.n,
                    side_want=args.side, eq_only=eq_only)
    if df.empty:
        print("no trades", file=sys.stderr); return 1
    print(f"  {len(df):,} trades, {df['yr'].nunique()} years")

    stop_table(df, A, args.side)
    target_table(df, A, args.side)
    grid_sim(df, A, args.side,
             outdir / f"exit_grid_n{args.n}_{args.side.lower()}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
