#!/usr/bin/env python3
"""
yoy_performance.py -- calendar-year performance for a chosen entry bar and stop.

Read-only. Writes CSVs. Touches no database tables.

    python yoy_performance.py --n 6 --stop-loss 10

RULES IMPLEMENTED
-----------------
Entry   open of bar N after signal_date, with the N>1 regime guard (no signal of
        any type may fire on bars 1..N-1).
Exit    whichever comes first: the stop, or the window end (next signal's date).
Stop    measured against the ORIGINAL entry fill, never against the year-start
        price. The calendar-year split below is accounting, not a new position.

YEAR BOUNDARY
-------------
"Artificial sell at year-end, artificial buy at the same price on 1 Jan" is
arithmetically identical to compounding daily returns inside each calendar year.
So the engine builds a daily equity curve and compounds it per year; every
metric below comes off that one object.

PORTFOLIO MODEL
---------------
Equal weight across whatever is open that day, rebalanced daily. Days with no
open position earn 0 (cash) and are included -- they drag Sharpe, correctly.
This matches the per-trade averages used everywhere else in the diagnostics.

METRICS
    return %        compounded portfolio return over the calendar year
    win rate        share of year-clipped trade segments finishing positive
    profit factor   gross positive segment P&L / |gross negative|
    sharpe          mean(daily) / stdev(daily) * sqrt(252)
    sortino         mean(daily) / downside deviation * sqrt(252)
    calmar          annual return / max intra-year drawdown
    hold p25/50/75/99   segment holding days (year-clipped, per your rule)

FILL ASSUMPTIONS
    --stop-fill level   fills exactly at -x%. Optimistic. Default, and matches
                        optimise_exits.py so the numbers are comparable.
    --stop-fill gap     fills at min(stop_level, bar_open) for LONG. Honest
                        about gap-downs; always worse.
    --cost-bps N        round-trip cost in basis points, charged on exit.
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


def simulate(sig_df, bars_df, censor, *, n: int, stop_pct: float | None,
             side_want: str, stop_fill: str, cost_bps: float):
    """Per-trade daily return streams under the entry/stop rules."""
    packs = {}
    for tid, grp in bars_df.groupby("ticker_id", sort=False):
        packs[tid] = (grp["d"].tolist(),
                      grp["open"].to_numpy(float), grp["high"].to_numpy(float),
                      grp["low"].to_numpy(float), grp["close"].to_numpy(float))

    d_idx_all, tr_all, ret_all = [], [], []
    trades = []
    tid_counter = 0
    skipped = {"regime_change_before_entry": 0, "entry_beyond_data": 0,
               "past_data_end": 0, "window_ends_before_entry": 0,
               "censored": 0, "bad_prices": 0}

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
                skipped["past_data_end"] += 1
                continue
            s_idx = e_idx + (n - 1)
            if s_idx >= nbars:
                skipped["entry_beyond_data"] += 1
                continue
            if n > 1 and i + 1 < total and \
                    recs[i + 1]["signal_date"] <= bar_dates[s_idx - 1]:
                skipped["regime_change_before_entry"] += 1
                continue

            if i + 1 < total:
                n_date, right_cens = recs[i + 1]["signal_date"], False
            else:
                n_date, right_cens = bar_dates[-1], True
            x_idx = bisect.bisect_right(bar_dates, n_date) - 1
            if x_idx < s_idx:
                skipped["window_ends_before_entry"] += 1
                continue

            e_date, x_date = bar_dates[s_idx], bar_dates[x_idx]
            if right_cens or any(e_date <= d <= x_date for d in cen):
                skipped["censored"] += 1
                continue

            entry = float(o[s_idx])
            hh, ll, cc, oo = h[s_idx:x_idx + 1], l[s_idx:x_idx + 1], \
                c[s_idx:x_idx + 1], o[s_idx:x_idx + 1]
            if entry <= 0 or (cc <= 0).any():
                skipped["bad_prices"] += 1
                continue

            is_long = side == LONG
            m = len(cc)

            # ---- locate the stop -----------------------------------------
            stop_bar, stop_px = -1, np.nan
            if stop_pct is not None:
                lvl = entry * (1 - stop_pct / 100) if is_long \
                    else entry * (1 + stop_pct / 100)
                if is_long:
                    hit = np.flatnonzero(ll <= lvl + abs(lvl) * _TOL)
                else:
                    hit = np.flatnonzero(hh >= lvl - abs(lvl) * _TOL)
                if hit.size:
                    stop_bar = int(hit[0])
                    if stop_fill == "gap":
                        ob = float(oo[stop_bar])
                        stop_px = min(lvl, ob) if is_long else max(lvl, ob)
                    else:
                        stop_px = lvl

            last = stop_bar if stop_bar >= 0 else m - 1
            exit_px = stop_px if stop_bar >= 0 else float(cc[last])

            # ---- daily marks ---------------------------------------------
            # bar 0 is measured from the entry fill, not the prior close.
            px = np.empty(last + 2)
            px[0] = entry
            px[1:last + 2] = cc[:last + 1]
            px[last + 1] = exit_px                    # final mark = the exit
            r = px[1:] / px[:-1] - 1.0
            if not is_long:
                r = -r
            if cost_bps:
                r[-1] -= cost_bps / 10000.0           # charge round trip on exit

            dates = bar_dates[s_idx:s_idx + last + 1]
            d_idx_all.append(np.array([d.toordinal() for d in dates]))
            tr_all.append(np.full(last + 1, tid_counter, dtype=np.int64))
            ret_all.append(r)

            gross = ((exit_px / entry - 1.0) if is_long
                     else (entry - exit_px) / entry) - cost_bps / 10000.0
            trades.append({"trade": tid_counter, "entry_date": e_date,
                           "exit_date": dates[-1], "bars": last + 1,
                           "stopped": stop_bar >= 0, "ret": gross})
            tid_counter += 1

    print("  skipped:", {k: v for k, v in skipped.items() if v}, file=sys.stderr)
    daily = pd.DataFrame({
        "ord": np.concatenate(d_idx_all),
        "trade": np.concatenate(tr_all),
        "ret": np.concatenate(ret_all),
    })
    return daily, pd.DataFrame(trades)


def yearly(daily: pd.DataFrame, tdf: pd.DataFrame, cal: np.ndarray) -> pd.DataFrame:
    daily["year"] = [date.fromordinal(int(x)).year for x in daily["ord"]]

    # equal weight across open positions; flat days earn 0
    port = daily.groupby("ord")["ret"].mean()
    port = port.reindex(cal, fill_value=0.0).sort_index()
    pyear = np.array([date.fromordinal(int(x)).year for x in port.index])

    # year-clipped segments: compound each trade's daily marks inside the year
    seg = daily.groupby(["trade", "year"])["ret"].agg(
        seg_ret=lambda s: float(np.prod(1.0 + s.to_numpy()) - 1.0), days="size"
    ).reset_index()

    rows = []
    for y in sorted(set(pyear)):
        r = port.to_numpy()[pyear == y]
        s = seg[seg["year"] == y]
        if r.size == 0 or s.empty:
            continue
        eq = np.cumprod(1.0 + r)
        total = eq[-1] - 1.0
        dd = eq / np.maximum.accumulate(eq) - 1.0
        mdd = float(-dd.min())

        sd = r.std(ddof=1)
        down = np.minimum(r, 0.0)
        dsd = float(np.sqrt(np.mean(down ** 2)))
        pos = s.loc[s["seg_ret"] > 0, "seg_ret"].sum()
        neg = -s.loc[s["seg_ret"] < 0, "seg_ret"].sum()
        hd = s["days"].to_numpy()

        rows.append({
            "year": y,
            "n_seg": len(s),
            "n_open": round(float(daily[daily["year"] == y]
                                  .groupby("ord")["trade"].size().mean()), 1),
            "ret_pct": 100 * total,
            "win_rate": 100 * float((s["seg_ret"] > 0).mean()),
            "profit_factor": float(pos / neg) if neg > 0 else np.nan,
            "sharpe": float(r.mean() / sd * np.sqrt(252)) if sd > 0 else np.nan,
            "sortino": float(r.mean() / dsd * np.sqrt(252)) if dsd > 0 else np.nan,
            "max_dd_pct": 100 * mdd,
            "calmar": float(total / mdd) if mdd > 0 else np.nan,
            "hold_p25": float(np.percentile(hd, 25)),
            "hold_p50": float(np.percentile(hd, 50)),
            "hold_p75": float(np.percentile(hd, 75)),
            "hold_p99": float(np.percentile(hd, 99)),
        })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--n", type=int, default=6)
    ap.add_argument("--stop-loss", type=float, default=10.0,
                    help="percent; use -1 for no stop")
    ap.add_argument("--side", choices=[LONG, SHORT], default=LONG)
    ap.add_argument("--stop-fill", choices=["level", "gap"], default="level")
    ap.add_argument("--cost-bps", type=float, default=0.0)
    ap.add_argument("--zone", default="Asia/Kolkata")
    ap.add_argument("--all-instruments", action="store_true")
    ap.add_argument("--outdir", default="diagnostics")
    ap.add_argument("--url", default=os.environ.get("TRIP_DB_URL", DEFAULT_URL))
    args = ap.parse_args()

    stop = None if args.stop_loss is not None and args.stop_loss < 0 else args.stop_loss
    eq_only = not args.all_instruments
    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    print(f"n={args.n}  stop_loss={'none' if stop is None else f'{stop}%'}  "
          f"side={args.side}  stop_fill={args.stop_fill}  "
          f"cost_bps={args.cost_bps}  eq_only={eq_only}")

    engine = create_engine(args.url)
    with engine.connect() as conn:
        print("loading signals...", file=sys.stderr)
        sig_df = pd.read_sql(SQL_SIGNALS, conn, params={"eq_only": eq_only})
        print("loading bars...", file=sys.stderr)
        bars_df = pd.read_sql(SQL_BARS, conn,
                              params={"zone": args.zone, "eq_only": eq_only})
        cen_df = pd.read_sql(SQL_CENSOR, conn)
    censor = {int(t): g["ex_date"].tolist() for t, g in cen_df.groupby("ticker_id")}
    cal = np.array(sorted({d.toordinal() for d in bars_df["d"].unique()}))

    print("simulating...", file=sys.stderr)
    daily, tdf = simulate(sig_df, bars_df, censor, n=args.n, stop_pct=stop,
                          side_want=args.side, stop_fill=args.stop_fill,
                          cost_bps=args.cost_bps)
    print(f"  {len(tdf):,} trades   {100*tdf['stopped'].mean():.1f}% stopped out")

    tbl = yearly(daily, tdf, cal)
    hdr = (f"{'year':>6}{'n_seg':>8}{'open':>7}{'ret%':>9}{'win%':>7}{'PF':>7}"
           f"{'sharpe':>8}{'sortino':>9}{'maxDD%':>8}{'calmar':>8}"
           f"{'p25':>6}{'p50':>6}{'p75':>7}{'p99':>7}")
    print("\n" + hdr); print("-" * len(hdr))
    for _, r in tbl.iterrows():
        print(f"{int(r['year']):>6}{int(r['n_seg']):>8,}{r['n_open']:>7.0f}"
              f"{r['ret_pct']:>9.2f}{r['win_rate']:>7.1f}{r['profit_factor']:>7.2f}"
              f"{r['sharpe']:>8.2f}{r['sortino']:>9.2f}{r['max_dd_pct']:>8.1f}"
              f"{r['calmar']:>8.2f}{r['hold_p25']:>6.0f}{r['hold_p50']:>6.0f}"
              f"{r['hold_p75']:>7.0f}{r['hold_p99']:>7.0f}")

    cagr = float(np.prod(1 + tbl["ret_pct"].to_numpy() / 100) ** (1 / len(tbl)) - 1)
    print("-" * len(hdr))
    print(f"{'CAGR':>6}{'':>8}{'':>7}{100*cagr:>9.2f}"
          f"{tbl['win_rate'].mean():>7.1f}{tbl['profit_factor'].median():>7.2f}"
          f"{tbl['sharpe'].median():>8.2f}{tbl['sortino'].median():>9.2f}"
          f"{tbl['max_dd_pct'].max():>8.1f}{tbl['calmar'].median():>8.2f}")
    print(f"\npositive years: {int((tbl['ret_pct'] > 0).sum())}/{len(tbl)}   "
          f"worst {tbl['ret_pct'].min():.1f}%   best {tbl['ret_pct'].max():.1f}%")

    tag = f"n{args.n}_sl{'none' if stop is None else stop}_{args.side.lower()}"
    tbl.to_csv(outdir / f"yoy_{tag}.csv", index=False)
    tdf.to_csv(outdir / f"trades_{tag}.csv", index=False)
    print(f"written: {outdir}/yoy_{tag}.csv  and  trades_{tag}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
