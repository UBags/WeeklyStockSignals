#!/usr/bin/env python3
"""READ-ONLY probe: re-ask yfinance for shares-outstanding.

`fundamentals_fetch_status` can only ever hold status='UNAVAILABLE' (there is a
CHECK constraint saying so), and it is written when fetch_shares() raises
SharesFetchPermanent. So once a row is there, "Yahoo has nothing" and "we
misread a throttle as an absence" look identical from the database.

This asks again, once per ticker, spaced by RequestGate, and prints what
yfinance ACTUALLY returns so the two can be told apart:

  EMPTY_INFO      info dict absent or has no symbol/name/quoteType
                  -> genuinely delisted or unknown. UNAVAILABLE was right.
  NO_SHARES_FIELD info dict is populated, but both sharesOutstanding and
                  impliedSharesOutstanding are null
                  -> Yahoo knows the ticker, has no share count.
                     UNAVAILABLE was right.
  RATE_LIMITED    -> UNAVAILABLE was WRONG. This is a throttle.
  TRANSIENT       -> UNAVAILABLE was WRONG, or the classifier fell through
                     its unknown-error branch.
  OK              -> UNAVAILABLE was WRONG. The data is there now.

Writes nothing. Safe to run while a backfill is going.

  python probe_shares.py                       # 5 tickers, UNAVAILABLE first
  python probe_shares.py --limit 10
  python probe_shares.py --symbols ITC,LT
  python probe_shares.py --gap 8
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
from trip_ema.fundamentals.ports import DEFAULT_SOURCE
from trip_ema.fundamentals.shares_fetch import (
    SharesFetchPermanent,
    SharesFetchRateLimited,
    SharesFetchTransient,
    fetch_shares,
)

# Tickers with no instrument_shares row. UNAVAILABLE ones come first --
# those are the ones whose verdict we are actually questioning.
PICK_SQL = """
SELECT k.ticker_id,
       k.symbol,
       fs.status,
       fs.attempts,
       fs.reason,
       fs.last_checked_at
  FROM tickers k
  LEFT JOIN instrument_shares s        ON s.ticker_id = k.ticker_id
  LEFT JOIN fundamentals_fetch_status fs
         ON fs.ticker_id = k.ticker_id AND fs.dataset = 'shares'
 WHERE k.exchange = :exchange
   AND k.is_active
   AND s.ticker_id IS NULL
 ORDER BY (fs.ticker_id IS NULL), k.symbol
 LIMIT :limit
"""

BY_SYMBOL_SQL = """
SELECT k.ticker_id, k.symbol, fs.status, fs.attempts, fs.reason,
       fs.last_checked_at
  FROM tickers k
  LEFT JOIN fundamentals_fetch_status fs
         ON fs.ticker_id = k.ticker_id AND fs.dataset = 'shares'
 WHERE k.exchange = :exchange AND k.symbol = ANY(:symbols)
 ORDER BY k.symbol
"""

INTERESTING_KEYS = (
    "symbol", "shortName", "quoteType", "currency", "exchange",
    "sharesOutstanding", "impliedSharesOutstanding", "floatShares",
    "marketCap", "regularMarketPrice",
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exchange", default="NSE")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--symbols", default="",
                    help="Comma-separated; overrides the auto-pick.")
    ap.add_argument("--gap", type=float, default=5.0,
                    help="Seconds between calls (default 5).")
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

        # --- raw info dict, before any classification ---
        gate.wait()
        info = None
        try:
            info = DEFAULT_SOURCE.ticker_info(yf_sym)
        except Exception as e:
            print(f"  RAW ticker_info raised: {type(e).__name__}: {e}")

        if info is None:
            pass
        elif not info:
            print("  RAW info: {} (empty dict)")
        else:
            print(f"  RAW info: {len(info)} keys")
            for k in INTERESTING_KEYS:
                if k in info:
                    print(f"      {k:28s} = {info[k]!r}")
            missing = [k for k in ("sharesOutstanding",
                                   "impliedSharesOutstanding")
                       if k not in info]
            if missing:
                print(f"      (absent entirely: {', '.join(missing)})")

        # --- what the repo's own fetcher decides ---
        gate.wait()
        try:
            si = fetch_shares(r.symbol, args.exchange)
            verdict = "OK"
            print(f"  fetch_shares -> {si.shares_outstanding:,} "
                  f"(source={si.source})")
        except SharesFetchRateLimited as e:
            verdict = "RATE_LIMITED"
            print(f"  fetch_shares -> RATE LIMITED: {e}")
        except SharesFetchTransient as e:
            verdict = "TRANSIENT"
            print(f"  fetch_shares -> TRANSIENT: {e}")
        except SharesFetchPermanent as e:
            msg = str(e)
            verdict = ("NO_SHARES_FIELD" if "neither shares" in msg
                       else "EMPTY_INFO")
            print(f"  fetch_shares -> PERMANENT: {e}")
        except Exception:
            verdict = "UNCLASSIFIED"
            print("  fetch_shares -> UNEXPECTED:")
            traceback.print_exc()

        verdicts.append((r.symbol, verdict))
        print()

    print("=" * 70)
    print("SUMMARY")
    for sym, v in verdicts:
        note = {
            "OK": "<- UNAVAILABLE was WRONG, data is there",
            "RATE_LIMITED": "<- UNAVAILABLE was WRONG, this is a throttle",
            "TRANSIENT": "<- UNAVAILABLE was WRONG (or classifier fell through)",
            "NO_SHARES_FIELD": "   Yahoo knows it, has no share count",
            "EMPTY_INFO": "   genuinely delisted/unknown",
        }.get(v, "")
        print(f"  {sym:14s} {v:16s} {note}")

    wrong = sum(1 for _, v in verdicts
                if v in {"OK", "RATE_LIMITED", "TRANSIENT"})
    print(f"\n{wrong}/{len(verdicts)} suggest the recorded verdict was wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
