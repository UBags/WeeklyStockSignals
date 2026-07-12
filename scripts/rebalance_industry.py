#!/usr/bin/env python3
"""Manual industry-index rebalance — operator script.

Deliberately NO DEFAULTS on any rebalance parameter. The operator must supply
every value explicitly, which is the confirmation: you cannot fire a rebalance
without stating exactly what you mean. Use this for off-schedule rebalances
(e.g. a constituent delisted mid-year due to merger/privatisation) — the normal
annual-April chain is built by the chain driver, not this script.

A rebalance is DESTRUCTIVE of the current period: it closes the currently-active
constituents (effective_to = date-1, REBALANCE_EXIT) and opens a new period from
`date`, re-picking the top-N as of `date`. It also computes + stores the
chain-link factor splicing the new basket onto the current index level.

PRECONDITIONS (the script checks and refuses if unmet):
  - daily_market_cap must be populated through `date` (else the pick is wrong).
  - The index must already exist (created by the build driver).

Usage (every argument is REQUIRED — no defaults):

  python scripts/rebalance_industry.py \\
      --industry "Specialty Chemicals" \\
      --exchange NSE \\
      --date 2019-07-15 \\
      --n 12 \\
      --min-constituents 4 \\
      --reason "PIIND delisted (merger) — re-pick basket" \\
      --confirm YES

`--confirm YES` (literal) is a final gate. Anything other than the exact string
YES aborts. This is intentional friction for a destructive, subscriber-visible
operation.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"--date {s!r} is not YYYY-MM-DD")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Manual off-schedule industry-index rebalance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # No defaults anywhere — every value must be supplied.
    p.add_argument("--industry", required=True,
                   help="industry_name exactly as in industry_index")
    p.add_argument("--exchange", required=True,
                   help="exchange for constituent selection, e.g. NSE")
    p.add_argument("--date", required=True,
                   help="rebalance date, YYYY-MM-DD (a trading day)")
    p.add_argument("--n", required=True, type=int,
                   help="number of constituents to pick (e.g. 12)")
    p.add_argument("--min-constituents", required=True, type=int,
                   help="minimum required; below this the rebalance is skipped")
    p.add_argument("--reason", required=True,
                   help="free-text reason (logged; e.g. delisting cause)")
    p.add_argument("--confirm", required=True,
                   help="must be the literal string YES to proceed")
    args = p.parse_args()

    if args.confirm != "YES":
        raise SystemExit("Aborted: --confirm must be exactly YES.")
    if args.n < args.min_constituents:
        raise SystemExit(
            f"Aborted: --n ({args.n}) < --min-constituents "
            f"({args.min_constituents})."
        )
    rebalance_date = _parse_date(args.date)

    # Imports deferred so --help works without a DB/config.
    from trip_ema.config import load_config
    from trip_ema.persistence.connection import build_engine_from_config
    from trip_ema.fundamentals.runners import run_rebalance

    config = load_config()
    db_engine = build_engine_from_config(config)

    # Precondition: index exists.
    from trip_ema.persistence.fundamentals_repo import IndustryIndexRepository
    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(args.industry)
    if idx is None:
        raise SystemExit(
            f"Aborted: index {args.industry!r} does not exist. Build it first."
        )

    # Precondition: mcap present on/through the rebalance date.
    from sqlalchemy import text
    with db_engine.connect() as conn:
        has_mcap = conn.execute(text("""
            SELECT 1 FROM daily_market_cap WHERE trade_date = :d LIMIT 1
        """), {"d": rebalance_date}).fetchone()
    if has_mcap is None:
        raise SystemExit(
            f"Aborted: no daily_market_cap rows on {rebalance_date}. "
            f"Populate market cap through this date first."
        )

    print(
        f"Rebalancing {args.industry!r} on {rebalance_date} "
        f"(exchange={args.exchange}, n={args.n}, "
        f"min={args.min_constituents}).\n"
        f"Reason: {args.reason}",
        flush=True,
    )
    result = run_rebalance(
        db_engine, args.industry, rebalance_date,
        n=args.n, exchange=args.exchange,
        min_constituents=args.min_constituents,
    )
    if getattr(result, "rebalance_skipped", False):
        print(
            f"SKIPPED: {getattr(result, 'skip_reason', 'below min')}. "
            f"No DB change.",
            flush=True,
        )
        return 1
    print(
        f"DONE: picked={result.constituents_picked} "
        f"closed_prior={result.constituents_closed}. "
        f"Chain-link factor stored; index value written for "
        f"{rebalance_date}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
