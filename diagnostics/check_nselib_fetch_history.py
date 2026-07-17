# import time
# from datetime import datetime, timedelta
# import pandas as pd
# from nselib import capital_market
#
#
# def get_20_year_data(symbol):
#     all_data = []
#
#     # Define start and end boundaries
#     end_date = datetime.today()
#     start_date = end_date - timedelta(days=20 * 365)
#
#     current_start = start_date
#     while current_start < end_date:
#         # Calculate 1-year window
#         current_end = min(current_start + timedelta(days=365), end_date)
#
#         # Format dates to string 'DD-MM-YYYY' required by nselib
#         str_start = current_start.strftime('%d-%m-%Y')
#         str_end = current_end.strftime('%d-%m-%Y')
#
#         print(f"Fetching {symbol} from {str_start} to {str_end}...")
#
#         try:
#             # Query the 1-year segment
#             df = capital_market.price_volume_data(
#                 symbol=symbol,
#                 from_date=str_start,
#                 to_date=str_end
#             )
#             if not df.empty:
#                 all_data.append(df)
#         except Exception as e:
#             print(f"Failed to fetch segment: {e}")
#
#         # Move window forward and pause to protect IP
#         current_start = current_end + timedelta(days=1)
#         time.sleep(2.5)
#
#         # Merge all historical slices into a single master sheet
#     if all_data:
#         master_df = pd.concat(all_data, ignore_index=True)
#         return master_df.drop_duplicates(subset=['Date'])
#     return pd.DataFrame()
#
# # Usage
# df_20_years = get_20_year_data("RELIANCE")

#!/usr/bin/env python3
"""Verify nselib serves RAW (unadjusted) NSE prices — the property yfinance does
NOT provide. For a known split, a raw series MUST show the unadjusted step on the
ex-date (close drops ~ price_factor). If it does, Option B (raw + self-adjust) is
viable and nselib becomes the single price+actions source.

nselib per-stock history:
  capital_market.price_volume_and_deliverable_position_data(symbol, from_date, to_date)
Dates are 'DD-MM-YYYY'. Column names vary by version, so we auto-detect the close
column and print what we found.

Run:  python verify_nselib_raw.py
"""
from __future__ import annotations

import time

import pandas as pd
from nselib import capital_market

# (symbol, ex_date 'YYYY-MM-DD', expected raw factor across the split)
CASES = [
    ("TITAGARH",  "2015-04-23", 0.2),
    ("SUZLON",    "2008-01-21", 0.2),
    ("TEXINFRA",  "2009-01-01", 0.1),
    ("SUTLEJTEX", "2017-09-26", 0.1),
    ("THYROCARE", "2025-11-28", 1 / 3),
]
TOL = 0.25


def nse_date(d: pd.Timestamp) -> str:
    return d.strftime("%d-%m-%Y")


def find_close_col(df: pd.DataFrame) -> str | None:
    for cand in ("ClosePrice", "Close Price", "CLOSE_PRICE", "close", "Close", "ClosePr"):
        if cand in df.columns:
            return cand
    for c in df.columns:
        if "clos" in c.lower():
            return c
    return None


def to_num(s):
    return pd.to_numeric(pd.Series(s).astype(str).str.replace(",", "", regex=False),
                         errors="coerce")


def main() -> None:
    for sym, ex_str, factor in CASES:
        print("=" * 80)
        print(f"{sym}  ex={ex_str}  expected RAW step ~ {factor:.3f}")
        ex = pd.Timestamp(ex_str)
        try:
            df = capital_market.price_volume_and_deliverable_position_data(
                symbol=sym,
                from_date=nse_date(ex - pd.Timedelta(days=10)),
                to_date=nse_date(ex + pd.Timedelta(days=10)),
            )
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed: {type(e).__name__}: {e}")
            time.sleep(1)
            continue
        if df is None or len(df) == 0:
            print("  no data")
            time.sleep(1)
            continue

        close_col = find_close_col(df)
        date_col = next((c for c in df.columns if "date" in c.lower()), df.columns[0])
        if close_col is None:
            print("  could not find a close column. columns:", list(df.columns))
            time.sleep(1)
            continue

        d = df[[date_col, close_col]].copy()
        d["dt"] = pd.to_datetime(d[date_col], errors="coerce", dayfirst=True)
        d["c"] = to_num(d[close_col])
        d = d.dropna(subset=["dt", "c"]).sort_values("dt").reset_index(drop=True)

        pos = int(d["dt"].searchsorted(ex))
        if not (0 < pos < len(d)):
            print("  ex-date outside returned window; cols:", list(df.columns))
            time.sleep(1)
            continue
        ratio = d.loc[pos, "c"] / d.loc[pos - 1, "c"]
        raw_ok = abs(ratio - factor) <= TOL
        verdict = "RAW CONFIRMED" if raw_ok else ("ALREADY-ADJUSTED?" if abs(ratio - 1) <= 0.2 else "OTHER")
        lo, hi = max(pos - 2, 0), min(pos + 3, len(d))
        for _, r in d.iloc[lo:hi].iterrows():
            mark = "<<" if r["dt"] == ex else "  "
            print(f"    {r['dt'].date()} {mark} close={r['c']:.2f}")
        print(f"    close[{close_col}] ratio={ratio:.3f} (raw wants ~{factor:.3f})  ->  {verdict}")
        time.sleep(1)


if __name__ == "__main__":
    main()
