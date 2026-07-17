#!/usr/bin/env python3
"""Confirm yfinance auto_adjust=False returns RAW (unadjusted) Close for NSE,
using splits/bonuses we KNOW from the nselib table. If raw is truly unadjusted,
the raw Close must show the split step (ratio ~ price_factor) on the ex-date,
while 'Adj Close' stays smooth (ratio ~ 1). If raw is secretly adjusted, both
would be smooth — which would sink the self-adjust plan.

Run:  python verify_auto_adjust_false.py
"""
from __future__ import annotations

import time

import pandas as pd
import yfinance as yf

# (symbol, ex_date, expected raw price_factor across the split) — from nselib.
CASES = [
    ("TITAGARH",  "2015-04-23", 0.2),      # Fv 10->2  (5:1)
    ("SUZLON",    "2008-01-21", 0.2),      # Fv 10->2  (5:1)
    ("TEXINFRA",  "2009-01-01", 0.1),      # Fv 10->1  (10:1)
    ("SUTLEJTEX", "2017-09-26", 0.1),      # Fv 10->1  (10:1)
    ("SILVERTUC", "2026-03-06", 0.5),      # Bonus 1:1
    ("THYROCARE", "2025-11-28", 1 / 3),    # Bonus 2:1
]
SUFFIX = ".NS"
TOL = 0.15


def main() -> None:
    print("yfinance", yf.__version__, "  (auto_adjust=False)\n")
    for sym, ex_str, factor in CASES:
        print("=" * 84)
        print(f"{sym}  ex={ex_str}  expected raw step ~ {factor:.3f}")
        try:
            df = yf.Ticker(sym + SUFFIX).history(
                period="max", interval="1d",
                auto_adjust=True, actions=True, repair=True,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed: {e}")
            time.sleep(1)
            continue
        if df is None or df.empty:
            print("  no data")
            time.sleep(1)
            continue
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = df.index.normalize()
        cols = {c.lower().replace(" ", "_"): c for c in df.columns}
        print(cols)
        has_adj = "adj_close" in cols
        ex = pd.Timestamp(ex_str)
        pos = int(df.index.searchsorted(ex))
        if not (0 < pos < len(df)):
            print("  ex-date outside coverage")
            time.sleep(1)
            continue
        raw_ratio = df.iloc[pos]["Close"] / df.iloc[pos - 1]["Close"]
        adj_ratio = (df.iloc[pos][cols["adj_close"]] / df.iloc[pos - 1][cols["adj_close"]]
                     if has_adj else float("nan"))
        raw_ok = abs(raw_ratio - factor) <= TOL * factor
        adj_ok = (not has_adj) or abs(adj_ratio - 1.0) <= TOL
        verdict = "RAW CONFIRMED" if (raw_ok and adj_ok) else "UNEXPECTED — investigate"
        lo, hi = max(pos - 2, 0), min(pos + 3, len(df))
        for d, row in df.iloc[lo:hi].iterrows():
            mark = "<<" if d == ex else "  "
            adjc = row[cols["adj_close"]] if has_adj else float("nan")
            spl = float(row.get("Stock Splits", 0.0) or 0.0)
            tag = f"  SPLIT={spl:g}" if spl else ""
            print(f"    {d.date()} {mark} raw_close={row['Close']:9.2f} "
                  f"adj_close={adjc:9.2f}{tag}")
        print(f"    raw_ratio={raw_ratio:.3f} (want ~{factor:.3f})  "
              f"adj_ratio={adj_ratio:.3f} (want ~1.0)  ->  {verdict}")
        time.sleep(1)


if __name__ == "__main__":
    main()