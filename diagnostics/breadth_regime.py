#!/usr/bin/env python3
"""
breadth_regime.py -- is open-position breadth a tradable regime signal?

Read-only. Writes CSVs. Touches no database tables.

    python breadth_regime.py --n 6 --stop-loss 40 --all-instruments

THE PROBLEM WITH THE ANNUAL READ
--------------------------------
Ranking 21 calendar years by average open positions looks decisive, but:

  * 21 observations cannot support a 95% claim. Bounding a probability under
    5% at 95% confidence needs ~60 observations (rule of three). The extreme
    buckets hold 3 and 4 years.
  * The investable universe grew ~8x over the sample, so raw position counts
    are not comparable across eras. This uses breadth = open / universe.
  * Same-year breadth and same-year return are both EFFECTS of the same price
    action -- positions close because the regime flipped because prices fell.
    Contemporaneous correlation is mechanical and untradable.

So this script measures FORWARD returns against breadth known at time t, at
daily resolution.

NO LOOK-AHEAD
-------------
Breadth is ranked by an EXPANDING-window percentile: each day's rank uses only
history up to and including that day, after a burn-in. Using the full-sample
distribution to define "high breadth" would leak the future.

OUTPUTS
    1. decile table  -- forward strategy and market returns by breadth decile
    2. threshold scan -- P(forward return > 0) with Wilson 95% intervals, so
       you can see directly where the interval stops containing 50%
    3. per-year breadth so you can tie back to the annual table
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

SQL_SIGNALS = text("""
SELECT e.ticker_id, e.timeframe, e.signal_date, e.signal_type
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


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval -- behaves at the extremes, unlike normal approx."""
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def simulate(sig_df, bars_df, censor, *, n: int, stop_pct: float | None,
             side_want: str):
    """Daily per-position returns under the entry/stop rules."""
    packs = {}
    for tid, grp in bars_df.groupby("ticker_id", sort=False):
        packs[tid] = (grp["d"].tolist(), grp["open"].to_numpy(float),
                      grp["high"].to_numpy(float), grp["low"].to_numpy(float),
                      grp["close"].to_numpy(float))

    ords, rets = [], []
    for (tk, _tf), sigs in sig_df.groupby(["ticker_id", "timeframe"], sort=False):
        pack = packs.get(tk)
        if pack is None:
            continue
        bar_dates, o, h, l, c = pack
        nbars = len(bar_dates)
        cen = censor.get(tk, [])
        recs = sigs.to_dict("records")
        total = len(recs)

        for i, sig in enumerate(recs):
            side = _WALK_SIDES.get(sig["signal_type"])
            if side is None or side != side_want:
                continue
            e_idx = bisect.bisect_right(bar_dates, sig["signal_date"])
            if e_idx >= nbars:
                continue
            s_idx = e_idx + (n - 1)
            if s_idx >= nbars:
                continue
            if n > 1 and i + 1 < total and \
                    recs[i + 1]["signal_date"] <= bar_dates[s_idx - 1]:
                continue
            if i + 1 < total:
                n_date, right_cens = recs[i + 1]["signal_date"], False
            else:
                n_date, right_cens = bar_dates[-1], True
            x_idx = bisect.bisect_right(bar_dates, n_date) - 1
            if x_idx < s_idx:
                continue
            e_date, x_date = bar_dates[s_idx], bar_dates[x_idx]
            if right_cens or any(e_date <= d <= x_date for d in cen):
                continue

            entry = float(o[s_idx])
            hh, ll, cc = h[s_idx:x_idx + 1], l[s_idx:x_idx + 1], c[s_idx:x_idx + 1]
            if entry <= 0 or (cc <= 0).any():
                continue
            is_long = side == LONG

            stop_bar, stop_px = -1, np.nan
            if stop_pct is not None:
                lvl = entry * (1 - stop_pct / 100) if is_long \
                    else entry * (1 + stop_pct / 100)
                hit = (np.flatnonzero(ll <= lvl + abs(lvl) * _TOL) if is_long
                       else np.flatnonzero(hh >= lvl - abs(lvl) * _TOL))
                if hit.size:
                    stop_bar, stop_px = int(hit[0]), lvl

            last = stop_bar if stop_bar >= 0 else len(cc) - 1
            exit_px = stop_px if stop_bar >= 0 else float(cc[last])
            px = np.empty(last + 2)
            px[0] = entry
            px[1:last + 2] = cc[:last + 1]
            px[last + 1] = exit_px
            r = px[1:] / px[:-1] - 1.0
            if not is_long:
                r = -r
            ords.append(np.array([d.toordinal()
                                  for d in bar_dates[s_idx:s_idx + last + 1]]))
            rets.append(r)

    return pd.DataFrame({"ord": np.concatenate(ords),
                         "ret": np.concatenate(rets)})


def market_series(bars_df: pd.DataFrame) -> pd.DataFrame:
    """Equal-weight daily return across every ticker with a bar -- market proxy,
    and the per-day ticker count used as the investable universe."""
    b = bars_df[["ticker_id", "d", "close"]].copy()
    b = b.sort_values(["ticker_id", "d"])
    b["r"] = b.groupby("ticker_id")["close"].pct_change()
    g = b.groupby("d").agg(mkt=("r", "mean"), universe=("ticker_id", "size"))
    g = g.reset_index()
    g["ord"] = [d.toordinal() for d in g["d"]]
    return g[["ord", "d", "mkt", "universe"]]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--n", type=int, default=6)
    ap.add_argument("--stop-loss", type=float, default=40.0,
                    help="percent; -1 for no stop")
    ap.add_argument("--side", choices=[LONG, SHORT], default=LONG)
    ap.add_argument("--horizons", default="21,63,126",
                    help="forward horizons in trading days")
    ap.add_argument("--burn-in", type=int, default=500,
                    help="days of history before percentiles are trusted")
    ap.add_argument("--zone", default="Asia/Kolkata")
    ap.add_argument("--all-instruments", action="store_true")
    ap.add_argument("--outdir", default="diagnostics")
    ap.add_argument("--url", default=os.environ.get("TRIP_DB_URL", DEFAULT_URL))
    args = ap.parse_args()

    stop = None if args.stop_loss < 0 else args.stop_loss
    eq_only = not args.all_instruments
    horizons = [int(x) for x in args.horizons.split(",")]
    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    print(f"n={args.n}  stop={'none' if stop is None else f'{stop}%'}  "
          f"side={args.side}  horizons={horizons}  eq_only={eq_only}")

    engine = create_engine(args.url)
    with engine.connect() as conn:
        print("loading...", file=sys.stderr)
        sig_df = pd.read_sql(SQL_SIGNALS, conn, params={"eq_only": eq_only})
        bars_df = pd.read_sql(SQL_BARS, conn,
                              params={"zone": args.zone, "eq_only": eq_only})
        cen_df = pd.read_sql(SQL_CENSOR, conn)
    censor = {int(t): g["ex_date"].tolist() for t, g in cen_df.groupby("ticker_id")}

    print("simulating...", file=sys.stderr)
    daily = simulate(sig_df, bars_df, censor, n=args.n, stop_pct=stop,
                     side_want=args.side)
    mkt = market_series(bars_df)

    agg = daily.groupby("ord").agg(port=("ret", "mean"), open_n=("ret", "size"))
    df = mkt.merge(agg, on="ord", how="left")
    df["port"] = df["port"].fillna(0.0)
    df["open_n"] = df["open_n"].fillna(0).astype(int)
    df = df.sort_values("ord").reset_index(drop=True)

    df["breadth"] = df["open_n"] / df["universe"]
    # expanding percentile: rank of today within history up to today only
    df["b_pct"] = df["breadth"].expanding().apply(
        lambda s: (s.to_numpy()[:-1] < s.to_numpy()[-1]).mean() * 100
        if len(s) > 1 else np.nan, raw=False)
    df.loc[: args.burn_in, "b_pct"] = np.nan

    for hz in horizons:
        df[f"fwd_s{hz}"] = (
            df["port"].shift(-1).rolling(hz).apply(
                lambda s: np.prod(1 + s) - 1, raw=True).shift(-(hz - 1)))
        df[f"fwd_m{hz}"] = (
            df["mkt"].shift(-1).rolling(hz).apply(
                lambda s: np.prod(1 + s) - 1, raw=True).shift(-(hz - 1)))

    d = df.dropna(subset=["b_pct"]).copy()
    print(f"  {len(d):,} usable days, {d['ord'].map(lambda o: date.fromordinal(o).year).nunique()} years")

    print("\n=== FORWARD RETURN BY BREADTH DECILE (expanding percentile) ===")
    d["dec"] = np.clip((d["b_pct"] // 10).astype(int), 0, 9)
    hdr = f"{'decile':>7}{'n_days':>9}{'breadth%':>10}"
    for hz in horizons:
        hdr += f"{'strat' + str(hz):>10}{'mkt' + str(hz):>9}{'P(s>0)':>8}"
    print(hdr); print("-" * len(hdr))
    for dec, g in d.groupby("dec"):
        line = f"{dec*10:>4}-{dec*10+10:<2}{len(g):>9,}{100*g['breadth'].mean():>10.1f}"
        for hz in horizons:
            s, mk = g[f"fwd_s{hz}"].dropna(), g[f"fwd_m{hz}"].dropna()
            line += (f"{100*s.mean():>10.2f}{100*mk.mean():>9.2f}"
                     f"{100*(s > 0).mean():>8.1f}" if len(s) else f"{'-':>27}")
        print(line)

    hz = horizons[-1]
    print(f"\n=== THRESHOLD SCAN, {hz}-day forward strategy return ===")
    print("    Wilson 95% CI. A claim is supportable only where the CI clears 50%.")
    hdr = (f"{'b_pct <=':>10}{'n':>8}{'P(>0)%':>9}{'CI_lo':>8}{'CI_hi':>8}"
           f"{'mean%':>9}   |{'b_pct >':>9}{'n':>8}{'P(>0)%':>9}"
           f"{'CI_lo':>8}{'CI_hi':>8}{'mean%':>9}")
    print(hdr); print("-" * len(hdr))
    for th in (10, 20, 30, 40, 50, 60, 70, 80, 90):
        lo_g = d[d["b_pct"] <= th][f"fwd_s{hz}"].dropna()
        hi_g = d[d["b_pct"] > th][f"fwd_s{hz}"].dropna()
        l1, l2 = wilson(int((lo_g > 0).sum()), len(lo_g))
        h1, h2 = wilson(int((hi_g > 0).sum()), len(hi_g))
        print(f"{th:>10}{len(lo_g):>8,}{100*(lo_g > 0).mean():>9.1f}"
              f"{100*l1:>8.1f}{100*l2:>8.1f}{100*lo_g.mean():>9.2f}   |"
              f"{th:>9}{len(hi_g):>8,}{100*(hi_g > 0).mean():>9.1f}"
              f"{100*h1:>8.1f}{100*h2:>8.1f}{100*hi_g.mean():>9.2f}")

    d["year"] = [date.fromordinal(int(o)).year for o in d["ord"]]
    print("\n=== PER-YEAR BREADTH (ties back to the annual table) ===")
    yr = d.groupby("year").agg(open_n=("open_n", "mean"),
                               universe=("universe", "mean"),
                               breadth=("breadth", "mean"),
                               b_pct=("b_pct", "mean"))
    yr["ret_pct"] = d.groupby("year")["port"].apply(lambda s: 100*(np.prod(1+s)-1))
    print(f"{'year':>6}{'open':>8}{'universe':>10}{'breadth%':>10}"
          f"{'b_pct':>8}{'ret%':>9}")
    print("-" * 51)
    for y, r in yr.iterrows():
        print(f"{y:>6}{r['open_n']:>8.0f}{r['universe']:>10.0f}"
              f"{100*r['breadth']:>10.1f}{r['b_pct']:>8.0f}{r['ret_pct']:>9.2f}")

    tag = f"n{args.n}_sl{'none' if stop is None else stop}"
    d.drop(columns=["d"]).to_csv(outdir / f"breadth_daily_{tag}.csv", index=False)
    yr.to_csv(outdir / f"breadth_yearly_{tag}.csv")
    print(f"\nwritten: {outdir}/breadth_daily_{tag}.csv, breadth_yearly_{tag}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
