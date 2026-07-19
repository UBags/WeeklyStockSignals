#!/usr/bin/env python3
"""READ-ONLY probe: re-ask yfinance for quarterly financials.

Same question as probe_shares.py, plus one that is specific to this dataset.

fetch_quarterly_financials() looks for revenue under a fixed candidate list --
"Total Revenue", "Revenues", "Revenue", "Revenue From Operations",
"Operating Revenue" -- and EBITDA under "EBITDA" / "Normalized EBITDA". If a
statement comes back populated but uses a row label outside those lists, the
fetcher raises FinancialsFetchPermanent and the ticker is recorded UNAVAILABLE
forever. That is indistinguishable in the database from Yahoo having nothing.

So this prints THE ACTUAL ROW LABELS of the returned statement. If you see a
populated income statement whose revenue row is named something not in the
candidate list, the fix is the candidate list, not the network.

Verdicts:
  EMPTY_STMT      no statement returned -> genuinely absent. Record was right.
  NO_MATCHING_ROW statement present, but no candidate revenue/EBITDA row
                  matched -> LOOK AT THE PRINTED ROW LABELS. Record may be
                  a field-naming miss, not an absence.
  RATE_LIMITED    -> record was WRONG, this is a throttle.
  TRANSIENT       -> record was WRONG.
  OK              -> record was WRONG, data is there.

Writes nothing. Safe to run while a backfill is going.

  python probe_financials.py                    # 5 tickers, UNAVAILABLE first
  python probe_financials.py --limit 10
  python probe_financials.py --symbols ITC,LT
"""
from __future__ import annotations

import argparse
import sys
import traceback

from sqlalchemy import text

from trip_ema.config import load_config
from trip_ema.persistence.connection import create_engine_from_config
from trip_ema.etl.exchanges import yfinance_symbol
from trip_ema.etl.request_gate import RequestGate
from trip_ema.fundamentals.financials_fetch import (
    EBITDA_FIELD_CANDIDATES,
    REVENUE_FIELD_CANDIDATES,
    FinancialsFetchPermanent,
    FinancialsFetchRateLimited,
    FinancialsFetchTransient,
    fetch_quarterly_financials,
)
from trip_ema.fundamentals.ports import DEFAULT_SOURCE

PICK_SQL = """
SELECT k.ticker_id,
       k.symbol,
       fs.status,
       fs.attempts,
       fs.reason,
       fs.last_checked_at
  FROM tickers k
  LEFT JOIN quarterly_financials q      ON q.ticker_id = k.ticker_id
  LEFT JOIN fundamentals_fetch_status fs
         ON fs.ticker_id = k.ticker_id AND fs.dataset = 'financials'
 WHERE k.exchange = :exchange
   AND k.is_active
   AND q.ticker_id IS NULL
 GROUP BY k.ticker_id, k.symbol, fs.ticker_id, fs.status, fs.attempts,
          fs.reason, fs.last_checked_at
 ORDER BY (fs.ticker_id IS NULL), k.symbol
 LIMIT :limit
"""

BY_SYMBOL_SQL = """
SELECT k.ticker_id, k.symbol, fs.status, fs.attempts, fs.reason,
       fs.last_checked_at
  FROM tickers k
  LEFT JOIN fundamentals_fetch_status fs
         ON fs.ticker_id = k.ticker_id AND fs.dataset = 'financials'
 WHERE k.exchange = :exchange AND k.symbol = ANY(:symbols)
 ORDER BY k.symbol
"""


def _dump_statement(df, currency: str) -> None:
    """Print the shape and, crucially, every row label."""
    if df is None:
        print("  RAW statement: None")
        return
    try:
        n_rows, n_cols = df.shape
    except Exception:
        print(f"  RAW statement: unexpected type {type(df)!r}")
        return
    if n_rows == 0 or n_cols == 0:
        print(f"  RAW statement: EMPTY (shape={df.shape}, currency={currency})")
        return

    print(f"  RAW statement: shape={df.shape} currency={currency}")
    try:
        cols = [str(c)[:10] for c in df.columns]
        print(f"  quarters: {', '.join(cols)}")
    except Exception:
        pass

    print("  row labels (THIS is what the candidate list must match):")
    rev_hits, ebitda_hits = [], []
    for label in df.index:
        s = str(label)
        mark = ""
        if s in REVENUE_FIELD_CANDIDATES:
            mark, _ = "  <== revenue candidate MATCH", rev_hits.append(s)
        elif s in EBITDA_FIELD_CANDIDATES:
            mark, _ = "  <== ebitda candidate MATCH", ebitda_hits.append(s)
        elif "revenue" in s.lower() or "income from operation" in s.lower():
            mark = "  <== revenue-LIKE but NOT in candidate list"
        elif "ebitda" in s.lower():
            mark = "  <== ebitda-LIKE but NOT in candidate list"
        print(f"      {s}{mark}")

    if not rev_hits:
        print("  !! no revenue row matched. Candidate list is:")
        print(f"     {', '.join(REVENUE_FIELD_CANDIDATES)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exchange", default="NSE")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--gap", type=float, default=5.0)
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    engine = create_engine_from_config(load_config(args.config))
    with engine.connect() as conn:
        if args.symbols:
            syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            rows = conn.execute(
                text(BY_SYMBOL_SQL),
                {"exchange": args.exchange, "symbols": syms},
            ).fetchall()
        else:
            rows = conn.execute(
                text(PICK_SQL),
                {"exchange": args.exchange, "limit": args.limit},
            ).fetchall()

    if not rows:
        print("No matching tickers. Nothing to probe.")
        return 0

    gate = RequestGate(args.gap)
    verdicts: list[tuple[str, str]] = []

    print(f"Probing {len(rows)} ticker(s), {args.gap}s apart. READ ONLY.\n")

    for r in rows:
        yf_sym = yfinance_symbol(r.symbol, args.exchange)
        print("=" * 70)
        print(f"{r.symbol}  (ticker_id={r.ticker_id})  ->  {yf_sym}")
        print(f"  recorded: status={r.status or '(no row)'} "
              f"attempts={r.attempts} last_checked={r.last_checked_at}")
        if r.reason:
            print(f"  reason  : {r.reason[:160]}")

        gate.wait()
        try:
            df, currency = DEFAULT_SOURCE.quarterly_financials(yf_sym)
            _dump_statement(df, currency)
        except Exception as e:
            print(f"  RAW quarterly_financials raised: {type(e).__name__}: {e}")

        gate.wait()
        try:
            out = fetch_quarterly_financials(r.symbol, args.exchange)
            verdict = "OK"
            print(f"  fetch_quarterly_financials -> {len(out)} quarter(s)")
            for q in out[:4]:
                print(f"      {q}")
        except FinancialsFetchRateLimited as e:
            verdict = "RATE_LIMITED"
            print(f"  fetch_quarterly_financials -> RATE LIMITED: {e}")
        except FinancialsFetchTransient as e:
            verdict = "TRANSIENT"
            print(f"  fetch_quarterly_financials -> TRANSIENT: {e}")
        except FinancialsFetchPermanent as e:
            msg = str(e).lower()
            verdict = ("NO_MATCHING_ROW" if "had either" in msg
                       else "EMPTY_STMT")
            print(f"  fetch_quarterly_financials -> PERMANENT: {e}")
        except Exception:
            verdict = "UNCLASSIFIED"
            print("  fetch_quarterly_financials -> UNEXPECTED:")
            traceback.print_exc()

        verdicts.append((r.symbol, verdict))
        print()

    print("=" * 70)
    print("SUMMARY")
    for sym, v in verdicts:
        note = {
            "OK": "<- record was WRONG, data is there",
            "RATE_LIMITED": "<- record was WRONG, this is a throttle",
            "TRANSIENT": "<- record was WRONG (or classifier fell through)",
            "NO_MATCHING_ROW": "<- CHECK the row labels printed above",
            "EMPTY_STMT": "   genuinely no statement",
        }.get(v, "")
        print(f"  {sym:14s} {v:16s} {note}")

    suspect = sum(1 for _, v in verdicts
                  if v in {"OK", "RATE_LIMITED", "TRANSIENT", "NO_MATCHING_ROW"})
    print(f"\n{suspect}/{len(verdicts)} warrant a second look.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
