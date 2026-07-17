#!/usr/bin/env python3
"""Fetch NSE bars fresh from yfinance with auto_adjust=True (production-equivalent:
same auto_adjust + repair as trip_ema.etl.yfinance_source) and print OHLCV in a
+/-2 trading-day window around each suspected seam, so we can diff against what's
stored in ohlcv_bars.

Reads nothing from the DB — pure yfinance. Prints the Stock Splits / Dividends
actions inside each window too; with auto_adjust=True the OHLC is already
split+dividend adjusted, so a genuine (properly handled) split shows a SPLIT tag
on the date but NO price jump. A price jump that survives here is yfinance's own
bad data, not an adjustment gap.

Run:  python verify_seams_yfinance.py
"""
from __future__ import annotations

import math
import time

import pandas as pd
import yfinance as yf

# (bare NSE symbol -> list of suspected seam dates). Edit freely.
EVENTS: dict[str, list[str]] = {
    "THYROCARE": ["2019-10-27", "2019-10-29", "2020-11-14", "2020-11-17"],
    "TIL":       ["2024-03-13", "2024-03-14", "2024-03-18"],
    "SURANASOL": ["2008-10-27", "2008-10-29", "2010-08-17"],
    "SUTLEJTEX": ["2006-06-12", "2006-12-14"],
    "SWANDEF":   ["2025-01-21"],
    "TEXRAIL":   ["2009-05-21", "2009-05-22", "2009-07-07",
                  "2009-07-13", "2009-07-14", "2010-11-01"],
    "SILVERTUC": ["2023-01-02", "2024-01-01", "2026-01-01"],
    "SKFINDIA":  ["2025-10-15"],
    "TITAGARH":  ["2008-04-22"],
    "SUZLON":    ["2008-10-24"],
    "STAR":      ["2024-12-06"],
    "TMPV":      ["2025-10-14"],
}

SUFFIX = ".NS"          # NSE. Use ".BO" for BSE-only names.
WINDOW = 2              # trading days each side of the seam
DIRTY_THRESHOLD = 0.40  # |close/prev_close - 1| above this = flagged
SLEEP_BETWEEN = 1.0     # be gentle with Yahoo (429 backoff)


def fetch(sym: str) -> pd.DataFrame | None:
    t = yf.Ticker(sym + SUFFIX)
    df = t.history(
        period="max",
        interval="1d",
        back_adjust=False,
        # auto_adjust=False,   # matches production (split+dividend adjusted)
        actions=True,       # keep Dividends + Stock Splits columns
        repair=True,        # matches production self._repair default
    )
    if df is None or df.empty:
        return None
    df = df.copy()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    df["c_ratio"] = df["Close"] / df["Close"].shift(1)
    return df


def fmt(x) -> str:
    return "   n/a" if (x is None or (isinstance(x, float) and math.isnan(x))) else f"{x:7.3f}"


def main() -> None:
    for sym, dates in EVENTS.items():
        print("=" * 96)
        print(sym)
        try:
            df = fetch(sym)
        except Exception as e:  # noqa: BLE001 - diagnostic, want the message
            print(f"  fetch failed: {e}")
            time.sleep(SLEEP_BETWEEN)
            continue
        if df is None:
            print("  no data from yfinance (delisted / wrong suffix / renamed?)")
            time.sleep(SLEEP_BETWEEN)
            continue

        first, last = df.index[0].date(), df.index[-1].date()
        print(f"  coverage: {first} .. {last}  ({len(df)} bars)")

        for ed_str in dates:
            ed = pd.Timestamp(ed_str)
            if ed < df.index[0] or ed > df.index[-1]:
                print(f"\n  event {ed.date()}: OUTSIDE yfinance coverage")
                continue
            pos = int(df.index.searchsorted(ed))
            if pos < len(df) and df.index[pos] == ed:
                center, exact = pos, True
            else:  # seam date was a holiday/missing bar; anchor on prior bar
                center, exact = max(pos - 1, 0), False

            lo, hi = max(center - WINDOW, 0), min(center + WINDOW + 1, len(df))
            win = df.iloc[lo:hi]
            note = "" if exact else "  (seam date has NO bar; anchored on prior)"
            cr = df.iloc[center]["c_ratio"]
            verdict = "DIRTY" if (pd.notna(cr) and abs(cr - 1) > DIRTY_THRESHOLD) else "clean"
            print(f"\n  event {ed.date()}  [{verdict}]{note}")

            for d, row in win.iterrows():
                mark = "<<" if d == ed else ("~~" if d == df.index[center] else "  ")
                split = float(row.get("Stock Splits", 0.0) or 0.0)
                div = float(row.get("Dividends", 0.0) or 0.0)
                tag = ""
                if split:
                    tag += f"  SPLIT={split:g}"
                if div:
                    tag += f"  DIV={div:g}"
                vol = int(row["Volume"]) if pd.notna(row["Volume"]) else 0
                print(
                    f"    {d.date()} {mark} "
                    f"O={row['Open']:9.2f} H={row['High']:9.2f} "
                    f"L={row['Low']:9.2f} C={row['Close']:9.2f} "
                    f"ratio={fmt(row['c_ratio'])} V={vol:>10d}{tag}"
                )
        time.sleep(SLEEP_BETWEEN)


if __name__ == "__main__":
    main()