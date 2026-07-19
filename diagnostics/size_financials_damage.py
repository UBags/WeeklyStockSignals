#!/usr/bin/env python3
"""READ-ONLY: how many 'financials UNAVAILABLE' verdicts are actually wrong?

Under rate limiting yfinance returns an EMPTY DataFrame rather than raising.
financials_fetch._is_empty() then reports FinancialsFetchPermanent, and the
sweep records UNAVAILABLE -- a verdict with no expiry, in a table whose CHECK
constraint permits no other status. A throttle and a genuine absence are
therefore written identically.

ITC, LT, CONCOR, IRCTC and TRENT are all in that cohort, and all five return
full statements on re-ask. So some fraction of the cohort is wrong. This takes
a RANDOM sample and measures the fraction, so the remediation is sized by
evidence rather than by the five names that happened to be checked.

Samples uniformly at random, not alphabetically -- the first probe hit
microcaps because it sorted by symbol, and microcaps are exactly the population
for which the verdict is most likely correct.

Writes nothing.

  python size_financials_damage.py                # sample 40
  python size_financials_damage.py --sample 100 --gap 3
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from trip_ema.config import load_config
from trip_ema.etl.exchanges import yfinance_symbol
from trip_ema.etl.request_gate import RequestGate
from trip_ema.fundamentals.financials_fetch import (
    FinancialsFetchPermanent,
    FinancialsFetchRateLimited,
    FinancialsFetchTransient,
    fetch_quarterly_financials,
)
from trip_ema.fundamentals.ports import DEFAULT_SOURCE
from trip_ema.persistence.connection import create_engine_from_config

SAMPLE_SQL = """
SELECT k.ticker_id, k.symbol
  FROM fundamentals_fetch_status fs
  JOIN tickers k ON k.ticker_id = fs.ticker_id
 WHERE fs.dataset = 'financials'
   AND fs.status  = 'UNAVAILABLE'
   AND k.exchange = :exchange
   AND k.is_active
 ORDER BY random()
 LIMIT :sample
"""

TOTAL_SQL = """
SELECT count(*)
  FROM fundamentals_fetch_status fs
  JOIN tickers k ON k.ticker_id = fs.ticker_id
 WHERE fs.dataset = 'financials' AND fs.status = 'UNAVAILABLE'
   AND k.exchange = :exchange AND k.is_active
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exchange", default="NSE")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--gap", type=float, default=5.0)
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    engine = create_engine_from_config(load_config(args.config))
    with engine.connect() as conn:
        total = conn.execute(
            text(TOTAL_SQL), {"exchange": args.exchange},
        ).scalar_one()
        rows = conn.execute(
            text(SAMPLE_SQL),
            {"exchange": args.exchange, "sample": args.sample},
        ).fetchall()

    if not rows:
        print("Nothing marked UNAVAILABLE for financials. Nothing to size.")
        return 0

    gate = RequestGate(args.gap)
    has_data: list[str] = []
    empty: list[str] = []
    throttled: list[str] = []
    other: list[str] = []

    est_secs = int(len(rows) * args.gap * 2)
    print(f"Cohort: {total} ticker(s) marked UNAVAILABLE (financials).")
    print(f"Sampling {len(rows)} at random, {args.gap}s apart "
          f"(~{est_secs // 60}m{est_secs % 60:02d}s). READ ONLY.\n")

    for i, r in enumerate(rows, 1):
        yf_sym = yfinance_symbol(r.symbol, args.exchange)
        gate.wait()
        try:
            out = fetch_quarterly_financials(r.symbol, args.exchange)
            has_data.append(r.symbol)
            tag = f"HAS DATA ({len(out)} quarters)  <- verdict WRONG"
        except FinancialsFetchRateLimited:
            throttled.append(r.symbol)
            tag = "RATE LIMITED  <- verdict WRONG"
        except FinancialsFetchTransient:
            throttled.append(r.symbol)
            tag = "TRANSIENT  <- verdict WRONG"
        except FinancialsFetchPermanent:
            # Distinguish empty-statement from every-quarter-unusable.
            gate.wait()
            try:
                df, _ = DEFAULT_SOURCE.quarterly_financials(yf_sym)
                n = 0 if df is None else int(getattr(df, "shape", (0, 0))[0])
            except Exception:
                n = -1
            if n > 0:
                other.append(r.symbol)
                tag = f"stmt has {n} rows but no usable quarter  <- CHECK"
            else:
                empty.append(r.symbol)
                tag = "empty statement  <- verdict correct"
        except Exception as e:
            other.append(r.symbol)
            tag = f"UNEXPECTED {type(e).__name__}  <- CHECK"

        print(f"  [{i:3d}/{len(rows)}] {r.symbol:14s} {tag}")

    n = len(rows)
    wrong = len(has_data) + len(throttled)
    rate = wrong / n

    print("\n" + "=" * 66)
    print(f"  data is there now : {len(has_data):4d}")
    print(f"  throttled/transient: {len(throttled):4d}")
    print(f"  genuinely empty   : {len(empty):4d}")
    print(f"  needs a look      : {len(other):4d}")
    print(f"\n  WRONG VERDICT RATE: {wrong}/{n} = {rate:.0%}")
    print(f"  Extrapolated over the cohort: ~{round(total * rate)} "
          f"of {total} tickers written off in error.")
    if has_data[:12]:
        print(f"\n  e.g. {', '.join(has_data[:12])}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
