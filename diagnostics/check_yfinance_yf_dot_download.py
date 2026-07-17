#!/usr/bin/env python3
"""Seam diagnostic via yf.download (matches production's ports.yf_download path)
instead of Ticker.history. Same auto_adjust/actions/repair as production. Prints
a +/-2 trading-day window around each suspected seam, plus a per-seam class so
you can scan which are bad ticks vs phantom bars vs real unadjusted splits vs
real moves.

Run:  python verify_seams_yf_download.py
"""
from __future__ import annotations

import math
import time

import pandas as pd
import yfinance as yf

# (bare NSE symbol -> suspected seam dates). Edit freely.
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

SUFFIX = ".NS"
WINDOW = 2
JUMP = 0.40           # |ratio-1| above this on the seam bar = flagged
REVERT_TOL = 0.25     # next close within this of prev close => spike-revert
SLEEP_BETWEEN = 1.0


def fetch(sym: str) -> pd.DataFrame | None:
    # Exactly the production call surface (trip_ema.etl.ports.yf_download ->
    # yf.download), with period="max" for the full history.
    raw = yf.download(
        tickers=[sym + SUFFIX],
        period="max",
        interval="1d",
        back_adjust=True,
        auto_adjust=True,
        # actions=True,
        repair=False,
        # threads=False,
        # progress=False,
        group_by="ticker",
    )
    if raw is None or raw.empty:
        return None

    # A single-ticker list still yields MultiIndex columns (ticker, field)
    # under group_by="ticker". Flatten to a plain OHLCV frame.
    if isinstance(raw.columns, pd.MultiIndex):
        key = sym + SUFFIX
        lvl0 = set(raw.columns.get_level_values(0))
        if key in lvl0:
            df = raw[key].copy()
        else:  # some versions put fields on level 0
            df = raw.droplevel(0, axis=1).copy()
    else:
        df = raw.copy()

    df = df.dropna(how="all")
    if df.empty:
        return None
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    # df["c_ratio"] = df["Adj Close"] / df["Adj Close"].shift(1)
    df["c_ratio"] = df["Close"] / df["Close"].shift(1)
    return df


def fmt(x) -> str:
    return "   n/a" if (x is None or (isinstance(x, float) and math.isnan(x))) else f"{x:7.3f}"


def _near(a, b, tol) -> bool:
    return (pd.notna(a) and pd.notna(b) and b != 0 and abs(a / b - 1) < tol)


def classify(df: pd.DataFrame, center: int) -> str:
    c = df.iloc[center]["Close"]
    cp = df.iloc[center - 1]["Close"] if center - 1 >= 0 else float("nan")
    cn = df.iloc[center + 1]["Close"] if center + 1 < len(df) else float("nan")
    vp = df.iloc[center - 1]["Volume"] if center - 1 >= 0 else float("nan")
    split = float(df.iloc[center].get("Stock Splits", 0.0) or 0.0)
    jumped = pd.notna(cp) and cp != 0 and abs(c / cp - 1) > JUMP
    if split:
        return f"SPLIT-META({split:g})"
    if pd.notna(vp) and vp == 0:
        return "PHANTOM(prev vol=0)"
    if jumped and _near(cn, cp, REVERT_TOL):
        return "SPIKE-REVERT(bad tick)"
    if jumped:
        return "STEP(unadjusted split? or real move)"
    return "ok"


def main() -> None:
    print("yfinance", yf.__version__, "— via yf.download\n")
    for sym, dates in EVENTS.items():
        print("=" * 96)
        print(sym)
        try:
            df = fetch(sym)
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed: {e}")
            time.sleep(SLEEP_BETWEEN)
            continue
        if df is None:
            print("  no data (delisted / wrong suffix / try .BO)")
            time.sleep(SLEEP_BETWEEN)
            continue

        print(f"  coverage: {df.index[0].date()} .. {df.index[-1].date()} ({len(df)} bars)")
        for ed_str in dates:
            ed = pd.Timestamp(ed_str)
            if ed < df.index[0] or ed > df.index[-1]:
                print(f"\n  event {ed.date()}: OUTSIDE coverage")
                continue
            pos = int(df.index.searchsorted(ed))
            if pos < len(df) and df.index[pos] == ed:
                center, exact = pos, True
            else:
                center, exact = max(pos - 1, 0), False
            cls = classify(df, center)
            note = "" if exact else "  (no bar on seam date; anchored prior)"
            print(f"\n  event {ed.date()}  [{cls}]{note}")
            lo, hi = max(center - WINDOW, 0), min(center + WINDOW + 1, len(df))
            for d, row in df.iloc[lo:hi].iterrows():
                mark = "<<" if d == ed else ("~~" if d == df.index[center] else "  ")
                split = float(row.get("Stock Splits", 0.0) or 0.0)
                div = float(row.get("Dividends", 0.0) or 0.0)
                tag = (f"  SPLIT={split:g}" if split else "") + (f"  DIV={div:g}" if div else "")
                vol = int(row["Volume"]) if pd.notna(row["Volume"]) else 0
                print(
                    f"    {d.date()} {mark} O={row['Open']:9.2f} H={row['High']:9.2f} "
                    f"L={row['Low']:9.2f} C={row['Close']:9.2f} "
                    # f"L={row['Low']:9.2f} C={row['Close']:9.2f} A_C={row['Adj Close']:9.2f} "
                    f"ratio={fmt(row['c_ratio'])} V={vol:>10d}{tag}"
                )
        time.sleep(SLEEP_BETWEEN)


if __name__ == "__main__":
    main()