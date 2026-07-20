#!/usr/bin/env python3
"""
strategy_backtest.py -- entry window + breadth gate + breadth-scaled exposure.

Read-only. Writes CSVs. Touches no database tables.

    python strategy_backtest.py --n 3 --min-breadth 45 --stop-loss 20 \
        --breadth-target 45 --max-leverage 1.6 --borrow-rate 10 --cost-bps 40

TWO SEPARATE USES OF BREADTH
----------------------------
  --min-breadth      GATE. Do not open a new position unless breadth clears
                     this. Controls WHICH trades are taken. -1 disables.
  --breadth-target   SIZING. invested_fraction = min(max_leverage,
                     breadth / target). Controls HOW MUCH capital is at risk.
                     0 disables (always 100% invested, the old behaviour).

The gate alone never reduces risk: holding 74 names instead of 108 does not
help when both baskets fall 60%. Only the sizing converts thin breadth into
cash.

    breadth  6% / target 45  -> 13% invested, 87% cash
    breadth 45% / target 45  -> 100% invested
    breadth 72% / target 45  -> 160% invested (capped by --max-leverage)

LEVERAGE COST
-------------
Exposure above 1.0 is borrowed and charged --borrow-rate (annual %, default 10,
roughly Indian margin funding). Idle cash earns --cash-rate (default 0).
Leverage without a funding cost is fantasy, so 10% is the default, not 0.

COSTS (--cost-bps)
------------------
Round-trip total in basis points, split across the two legs: half charged
against the entry price on day one, half against the exit price on the last
day. 40 = 20bps buy + 20bps sell. NOT per leg.

BREADTH DEFINITION
------------------
Tickers in an active BUY regime, as a percent of the LISTED universe -- every
ticker whose first bar <= date <= last bar. Counting only tickers that printed
a bar that day made the denominator collapse on sparse dates and produced
breadth above 100%. Universe below --min-universe is treated as unusable.

Computed from ALL signals regardless of what is traded, and lagged one trading
day: you buy at the open, so you can only know yesterday's regime count.

YEAR BOUNDARY
-------------
Marked flat at 31 Dec, re-entered at the same price on 1 Jan -- identical to
compounding daily returns inside each calendar year.
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
 ORDER BY e.ticker_id, e.timeframe, e.signal_date
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


def listed_universe(packs, cal_idx) -> np.ndarray:
    """Tickers LISTED on each day: first_bar <= day <= last_bar.

    Counting only tickers that printed a bar that day collapses on sparse
    dates and yields breadth > 100%.
    """
    diff = np.zeros(len(cal_idx) + 2, dtype=np.int64)
    for _tk, pack in packs.items():
        bd = pack[0]
        a, b = cal_idx.get(bd[0].toordinal()), cal_idx.get(bd[-1].toordinal())
        if a is None or b is None:
            continue
        diff[a] += 1
        diff[b + 1] -= 1
    return np.cumsum(diff[:len(cal_idx)])


def build_regimes(sig_df, packs, cal_idx, side_want) -> np.ndarray:
    """Active regimes per day, from ALL signals. Exogenous to what we trade."""
    diff = np.zeros(len(cal_idx) + 2, dtype=np.int64)
    for (tk, _tf), sigs in sig_df.groupby(["ticker_id", "timeframe"], sort=False):
        pack = packs.get(tk)
        if pack is None:
            continue
        bar_dates = pack[0]
        nb = len(bar_dates)
        recs = sigs.to_dict("records")
        for i, sig in enumerate(recs):
            if _WALK_SIDES.get(sig["signal_type"]) != side_want:
                continue
            e = bisect.bisect_right(bar_dates, sig["signal_date"])
            if e >= nb:
                continue
            nd = recs[i + 1]["signal_date"] if i + 1 < len(recs) else bar_dates[-1]
            x = bisect.bisect_right(bar_dates, nd) - 1
            if x < e:
                continue
            a, b = cal_idx.get(bar_dates[e].toordinal()), cal_idx.get(bar_dates[x].toordinal())
            if a is None or b is None:
                continue
            diff[a] += 1
            diff[b + 1] -= 1
    return np.cumsum(diff[:len(cal_idx)])


def run(sig_df, packs, censor, cal_idx, breadth_prev, *, n_list, min_breadth,
        stop_pct, side_want, stop_fill, cost_bps):
    ords, trs, rets = [], [], []
    trades, tid = [], 0
    skip = {"regime_flip": 0, "breadth_gate": 0, "past_data_end": 0,
            "censored": 0, "bad_prices": 0}
    half = cost_bps / 2.0 / 10000.0

    for (tk, _tf), sigs in sig_df.groupby(["ticker_id", "timeframe"], sort=False):
        pack = packs.get(tk)
        if pack is None:
            continue
        bar_dates, o, h, l, c = pack
        nb = len(bar_dates)
        cen = censor.get(tk, [])
        recs = sigs.to_dict("records")
        total = len(recs)

        for i, sig in enumerate(recs):
            side = _WALK_SIDES.get(sig["signal_type"])
            if side is None or side != side_want:
                continue
            e_idx = bisect.bisect_right(bar_dates, sig["signal_date"])
            if e_idx >= nb:
                skip["past_data_end"] += 1
                continue
            nxt = recs[i + 1]["signal_date"] if i + 1 < total else None

            s_idx, why = None, None
            for k in n_list:
                cand = e_idx + (k - 1)
                if cand >= nb:
                    why = "past_data_end"
                    break
                if k > 1 and nxt is not None and nxt <= bar_dates[cand - 1]:
                    why = "regime_flip"          # monotone; no later k can pass
                    break
                gi = cal_idx.get(bar_dates[cand].toordinal())
                if gi is None or gi == 0:
                    continue
                if breadth_prev[gi] > min_breadth:
                    s_idx = cand
                    break
                why = "breadth_gate"
            if s_idx is None:
                skip[why or "breadth_gate"] += 1
                continue

            if nxt is not None:
                x_idx, right_cens = bisect.bisect_right(bar_dates, nxt) - 1, False
            else:
                x_idx, right_cens = nb - 1, True
            if x_idx < s_idx:
                skip["regime_flip"] += 1
                continue
            e_date, x_date = bar_dates[s_idx], bar_dates[x_idx]
            if right_cens or any(e_date <= d <= x_date for d in cen):
                skip["censored"] += 1
                continue

            entry = float(o[s_idx])
            hh, ll, cc, oo = (h[s_idx:x_idx + 1], l[s_idx:x_idx + 1],
                              c[s_idx:x_idx + 1], o[s_idx:x_idx + 1])
            if entry <= 0 or (cc <= 0).any():
                skip["bad_prices"] += 1
                continue
            is_long = side == LONG

            stop_bar, stop_px = -1, np.nan
            if stop_pct is not None:
                lvl = entry * (1 - stop_pct / 100) if is_long \
                    else entry * (1 + stop_pct / 100)
                hit = (np.flatnonzero(ll <= lvl + abs(lvl) * _TOL) if is_long
                       else np.flatnonzero(hh >= lvl - abs(lvl) * _TOL))
                if hit.size:
                    stop_bar = int(hit[0])
                    ob = float(oo[stop_bar])
                    stop_px = (min(lvl, ob) if is_long else max(lvl, ob)) \
                        if stop_fill == "gap" else lvl

            last = stop_bar if stop_bar >= 0 else len(cc) - 1
            exit_px = stop_px if stop_bar >= 0 else float(cc[last])
            px = np.empty(last + 2)
            px[0] = entry
            px[1:last + 2] = cc[:last + 1]
            px[last + 1] = exit_px
            r = px[1:] / px[:-1] - 1.0
            if not is_long:
                r = -r
            # half the round trip on each leg, against that leg's own notional
            if half:
                r[0] -= half
                r[-1] -= half

            dd = bar_dates[s_idx:s_idx + last + 1]
            ords.append(np.array([x.toordinal() for x in dd]))
            trs.append(np.full(last + 1, tid, dtype=np.int64))
            rets.append(r)
            # compound the daily stream rather than subtracting costs from the
            # gross: costs are multiplicative, so a simple subtraction drifts
            # from the equity curve on large winners.
            net = float(np.prod(1.0 + r) - 1.0)
            trades.append({"trade": tid, "entry_bar_n": s_idx - e_idx + 1,
                           "entry_date": e_date, "exit_date": dd[-1],
                           "bars": last + 1, "stopped": stop_bar >= 0,
                           "ret": net})
            tid += 1

    if not ords:
        return pd.DataFrame(), pd.DataFrame(), skip
    return (pd.DataFrame({"ord": np.concatenate(ords),
                          "trade": np.concatenate(trs),
                          "ret": np.concatenate(rets)}),
            pd.DataFrame(trades), skip)


def portfolio(daily, cal, breadth_prev, *, target, max_lev, cash_rate,
              borrow_rate):
    """Daily portfolio return with breadth-scaled exposure and funding costs."""
    g = daily.groupby("ord")["ret"].agg(basket="mean", n_open="size")
    b = g["basket"].reindex(cal, fill_value=0.0).sort_index().to_numpy()
    n_open = g["n_open"].reindex(cal, fill_value=0).sort_index().to_numpy()

    if target and target > 0:
        f = np.minimum(max_lev, breadth_prev / float(target))
    else:
        f = np.ones(len(cal))
    f = np.where(n_open > 0, f, 0.0)          # nothing to hold -> all cash

    cash_d = cash_rate / 100.0 / 252.0
    borr_d = borrow_rate / 100.0 / 252.0
    idle = 1.0 - f
    funding = np.where(idle >= 0, idle * cash_d, idle * borr_d)
    port = f * b + funding
    port = np.maximum(port, -0.99)            # ruin guard
    wt = np.where(n_open > 0, f / np.maximum(n_open, 1), 0.0)
    return port, f, n_open, wt


def yearly(daily, cal, port, f, n_open, wt):
    pyear = np.array([date.fromordinal(int(x)).year for x in cal])
    d = daily.copy()
    d["year"] = [date.fromordinal(int(x)).year for x in d["ord"]]
    seg = d.groupby(["trade", "year"])["ret"].agg(
        seg_ret=lambda s: float(np.prod(1 + s.to_numpy()) - 1), days="size"
    ).reset_index()

    rows = []
    for y in sorted(set(pyear)):
        m = pyear == y
        r = port[m]
        s = seg[seg["year"] == y]
        if r.size == 0 or s.empty:
            continue
        eq = np.cumprod(1 + r)
        tot = eq[-1] - 1
        mdd = float(-(eq / np.maximum.accumulate(eq) - 1).min())
        sd = r.std(ddof=1)
        dsd = float(np.sqrt(np.mean(np.minimum(r, 0) ** 2)))
        pos = s.loc[s.seg_ret > 0, "seg_ret"].sum()
        neg = -s.loc[s.seg_ret < 0, "seg_ret"].sum()
        rows.append({"year": y, "n_seg": len(s),
                     "open": float(n_open[m].mean()),
                     "expo": float(f[m].mean()), "expo_max": float(f[m].max()),
                     "max_wt": 100 * float(wt[m].max()),
                     "ret_pct": 100 * tot,
                     "win_rate": 100 * float((s.seg_ret > 0).mean()),
                     "profit_factor": float(pos / neg) if neg > 0 else np.nan,
                     "sharpe": float(r.mean() / sd * np.sqrt(252)) if sd > 0 else np.nan,
                     "sortino": float(r.mean() / dsd * np.sqrt(252)) if dsd > 0 else np.nan,
                     "max_dd_pct": 100 * mdd,
                     "calmar": float(tot / mdd) if mdd > 0 else np.nan,
                     "hold_p50": float(np.percentile(s["days"], 50))})
    return pd.DataFrame(rows)


def show(tbl, label):
    hdr = (f"{'year':>6}{'n_seg':>8}{'open':>7}{'expo':>7}{'maxwt%':>8}"
           f"{'ret%':>9}{'win%':>7}{'PF':>7}{'sharpe':>8}{'maxDD%':>8}"
           f"{'calmar':>8}{'p50':>6}")
    print(f"\n=== {label} ===")
    print(hdr); print("-" * len(hdr))
    for _, r in tbl.iterrows():
        print(f"{int(r.year):>6}{int(r.n_seg):>8,}{r['open']:>7.0f}"
              f"{r.expo:>7.2f}{r.max_wt:>8.1f}{r.ret_pct:>9.2f}{r.win_rate:>7.1f}"
              f"{r.profit_factor:>7.2f}{r.sharpe:>8.2f}{r.max_dd_pct:>8.1f}"
              f"{r.calmar:>8.2f}{r.hold_p50:>6.0f}")
    cagr = float(np.prod(1 + tbl.ret_pct.to_numpy() / 100) ** (1 / len(tbl)) - 1)
    print("-" * len(hdr))
    print(f"  CAGR {100*cagr:>6.2f}%   positive years "
          f"{int((tbl.ret_pct > 0).sum())}/{len(tbl)}   "
          f"worst {tbl.ret_pct.min():.1f}%   best {tbl.ret_pct.max():.1f}%   "
          f"maxDD {tbl.max_dd_pct.max():.1f}%   "
          f"median Calmar {tbl.calmar.median():.2f}   "
          f"avg exposure {tbl.expo.mean():.2f}x")
    return cagr


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--n", default="3", help="candidate entry bars, e.g. 3,4,5")
    ap.add_argument("--min-breadth", type=float, default=45.0,
                    help="entry gate, percent of listed universe; -1 disables")
    ap.add_argument("--breadth-target", type=float, default=45.0,
                    help="breadth giving 1.0x exposure; 0 = always 100%% invested")
    ap.add_argument("--max-leverage", type=float, default=1.6)
    ap.add_argument("--borrow-rate", type=float, default=10.0,
                    help="annual %% on exposure above 1.0x")
    ap.add_argument("--cash-rate", type=float, default=0.0,
                    help="annual %% earned on idle cash")
    ap.add_argument("--stop-loss", type=float, default=20.0, help="-1 to disable")
    ap.add_argument("--cost-bps", type=float, default=0.0,
                    help="ROUND TRIP total bps, split half per leg")
    ap.add_argument("--min-universe", type=int, default=100)
    ap.add_argument("--side", choices=[LONG, SHORT], default=LONG)
    ap.add_argument("--stop-fill", choices=["level", "gap"], default="level")
    ap.add_argument("--no-compare", action="store_true")
    ap.add_argument("--zone", default="Asia/Kolkata")
    ap.add_argument("--all-instruments", action="store_true")
    ap.add_argument("--outdir", default="diagnostics")
    ap.add_argument("--url", default=os.environ.get("TRIP_DB_URL", DEFAULT_URL))
    args = ap.parse_args()

    n_list = sorted({int(x) for x in args.n.replace(" ", "").split(",")})
    stop = None if args.stop_loss < 0 else args.stop_loss
    eq_only = not args.all_instruments
    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    print(f"n={n_list}  gate>{args.min_breadth:g}%  target={args.breadth_target:g}% "
          f"maxlev={args.max_leverage:g}x  borrow={args.borrow_rate:g}%  "
          f"cash={args.cash_rate:g}%  SL={'none' if stop is None else f'{stop}%'}  "
          f"cost={args.cost_bps:g}bps RT  eq_only={eq_only}")

    engine = create_engine(args.url)
    with engine.connect() as conn:
        print("loading...", file=sys.stderr)
        sig_df = pd.read_sql(SQL_SIGNALS, conn, params={"eq_only": eq_only})
        bars_df = pd.read_sql(SQL_BARS, conn,
                              params={"zone": args.zone, "eq_only": eq_only})
        cen_df = pd.read_sql(SQL_CENSOR, conn)
    censor = {int(t): g["ex_date"].tolist() for t, g in cen_df.groupby("ticker_id")}

    packs = {}
    for tk, g in bars_df.groupby("ticker_id", sort=False):
        packs[tk] = (g["d"].tolist(), g["open"].to_numpy(float),
                     g["high"].to_numpy(float), g["low"].to_numpy(float),
                     g["close"].to_numpy(float))
    cal = np.array(sorted({d.toordinal() for d in bars_df["d"].unique()}))
    cal_idx = {int(o): i for i, o in enumerate(cal)}

    print("building breadth...", file=sys.stderr)
    regimes = build_regimes(sig_df, packs, cal_idx, args.side)
    uni = listed_universe(packs, cal_idx)
    breadth = np.where(uni >= args.min_universe,
                       100.0 * regimes / np.maximum(uni, 1), 0.0)
    breadth_prev = np.concatenate([[0.0], breadth[:-1]])
    print(f"  universe (listed): median {int(np.median(uni)):,}  max {uni.max():,}")
    print(f"  breadth: min {breadth.min():.1f}%  median {np.median(breadth):.1f}%"
          f"  max {breadth.max():.1f}%   days clearing gate: "
          f"{100*(breadth_prev > args.min_breadth).mean():.1f}%")

    print("simulating...", file=sys.stderr)
    daily, tdf, skip = run(sig_df, packs, censor, cal_idx, breadth_prev,
                           n_list=n_list, min_breadth=args.min_breadth,
                           stop_pct=stop, side_want=args.side,
                           stop_fill=args.stop_fill, cost_bps=args.cost_bps)
    if daily.empty:
        print("no trades survived", file=sys.stderr)
        return 1
    print(f"  {len(tdf):,} trades  {100*tdf.stopped.mean():.1f}% stopped  "
          f"entry mix {dict(tdf.entry_bar_n.value_counts().sort_index())}")
    print(f"  skipped: {dict(sorted(skip.items(), key=lambda kv: -kv[1]))}")

    port, f, n_open, wt = portfolio(daily, cal, breadth_prev,
                                    target=args.breadth_target,
                                    max_lev=args.max_leverage,
                                    cash_rate=args.cash_rate,
                                    borrow_rate=args.borrow_rate)
    tbl = yearly(daily, cal, port, f, n_open, wt)
    cagr = show(tbl, f"SIZED  n={n_list} gate>{args.min_breadth:g} "
                     f"target={args.breadth_target:g} lev<={args.max_leverage:g}x")

    if not args.no_compare:
        p0, f0, n0, w0 = portfolio(daily, cal, breadth_prev, target=0,
                                   max_lev=1.0, cash_rate=0.0, borrow_rate=0.0)
        t0 = yearly(daily, cal, p0, f0, n0, w0)
        c0 = show(t0, "CONTROL  same trades, always 100% invested, no sizing")
        m = tbl[["year", "ret_pct", "max_dd_pct"]].merge(
            t0[["year", "ret_pct", "max_dd_pct"]], on="year", suffixes=("_s", "_c"))
        print(f"\n{'year':>6}{'sized%':>10}{'flat%':>10}{'delta':>9}"
              f"{'sizedDD':>9}{'flatDD':>8}")
        print("-" * 52)
        for _, r in m.iterrows():
            print(f"{int(r.year):>6}{r.ret_pct_s:>10.2f}{r.ret_pct_c:>10.2f}"
                  f"{r.ret_pct_s - r.ret_pct_c:>9.2f}"
                  f"{r.max_dd_pct_s:>9.1f}{r.max_dd_pct_c:>8.1f}")
        print(f"\n  CAGR sized {100*cagr:.2f}%  vs flat {100*c0:.2f}%   "
              f"maxDD {tbl.max_dd_pct.max():.1f}% vs {t0.max_dd_pct.max():.1f}%")

    tag = (f"n{'-'.join(map(str, n_list))}_g{args.min_breadth:g}"
           f"_t{args.breadth_target:g}_L{args.max_leverage:g}_sl{stop}")
    tbl.to_csv(outdir / f"bt_yoy_{tag}.csv", index=False)
    tdf.to_csv(outdir / f"bt_trades_{tag}.csv", index=False)
    print(f"\nwritten: {outdir}/bt_yoy_{tag}.csv, bt_trades_{tag}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
