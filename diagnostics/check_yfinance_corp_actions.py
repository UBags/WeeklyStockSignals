#!/usr/bin/env python3
"""Fetch corporate actions (splits + dividends) via yfinance's Ticker.splits /
Ticker.dividends for the NSE names we're investigating. Prints them per ticker
and, at the end, a consolidated CSV block shaped for the corporate_actions table
(symbol, effective_date, action_type, ratio_numerator, ratio_denominator, amount).

Split value convention (yfinance): a 2-for-1 split -> 2.0 ; a 1-for-2 reverse ->
0.5 ; a 1:10 -> 10.0 or 0.1 depending on direction. We decompose the float into
numerator:denominator with Fraction so it drops into ratio_numerator/denominator.

Run:  python fetch_actions.py  > actions_dump.txt
"""
from __future__ import annotations

import time
from fractions import Fraction

import pandas as pd
import yfinance as yf

SUFFIX = ".NS"
SLEEP = 0.8

TICKERS = [
    "THYROCARE", "TIL", "SURANASOL", "SURANAT&P", "SUTLEJTEX", "SWANDEF",
    "TEXRAIL", "TEXINFRA", "SILVERTUC", "SILVER1", "SILINV", "SKFINDIA",
    "TITAGARH", "SUZLON", "STAR", "TMPV", "SHREYANIND", "SICAGEN", "SPARC",
    "SPCENET", "STEELCITY", "SUMEETINDS", "SUMMITSEC", "SUVEN", "TICL",
    "TIMKEN", "TVSHLTD", "UMESLTD",
]


def to_date(ts):
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_localize(None)
    return t.date()


def as_ratio(v: float) -> tuple[int, int]:
    fr = Fraction(float(v)).limit_denominator(1000)
    return fr.numerator, fr.denominator


def main() -> None:
    print("yfinance", yf.__version__, "\n")
    rows: list[tuple] = []

    for sym in TICKERS:
        print("=" * 72)
        print(sym)
        try:
            tk = yf.Ticker(sym + SUFFIX)
            splits = tk.splits
            dividends = tk.dividends
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed: {type(e).__name__}: {e}")
            time.sleep(SLEEP)
            continue

        if splits is None or len(splits) == 0:
            print("  splits:    none")
        else:
            print(f"  splits:    {len(splits)}")
            for ts, val in splits.items():
                if not val:
                    continue
                d = to_date(ts)
                num, den = as_ratio(val)
                print(f"    {d}  value={val:g}  ({num}:{den})")
                rows.append((sym, d, "split", num, den, ""))

        if dividends is None or len(dividends) == 0:
            print("  dividends: none")
        else:
            print(f"  dividends: {len(dividends)}")
            for ts, val in dividends.items():
                if not val:
                    continue
                d = to_date(ts)
                print(f"    {d}  amount={val:g}")
                rows.append((sym, d, "dividend", "", "", f"{val:g}"))

        time.sleep(SLEEP)

    print("\n\n#### CSV  symbol,effective_date,action_type,ratio_numerator,"
          "ratio_denominator,amount ####")
    for r in rows:
        print(",".join("" if x == "" else str(x) for x in r))
    print(f"#### {len(rows)} action rows total ####")


if __name__ == "__main__":
    main()