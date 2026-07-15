#!/usr/bin/env python3
"""Straggler re-fetch — recover tickers the batched backfill dropped.

Problem: yfinance intermittently returns an empty frame for some symbols
inside a large batched yf.download (batch_size=50), mislabeling them
"possibly delisted; no price data found" even for liquid names (KARURVYSYA,
CONCOR, STLTECH, ...). The batch still "succeeds" overall, so the per-symbol
drop is silent and those tickers end up with < 30 bars. Fetched SOLO a moment
later, the same symbols return full history — it's batch-load flakiness, not a
real data gap.

This script finds every active EQ ticker on `exchange` with fewer than
`--min-bars` daily bars and re-fetches them ONE AT A TIME (batch_size=1) via
the pipeline's own fetch_bars_for_tickers — same reconcile+write path, just
reliable solo calls with a delay between each. Reports which recovered.

Idempotent: safe to run repeatedly; upserts bars, only re-fetches whatever is
still short. Run AFTER the main backfill's fetch stage completes.

Usage (no defaults on the operational knobs):

  python scripts/refetch_stragglers.py \\
      --exchange NSE \\
      --date-from 2006-04-01 \\
      --min-bars 30 \\
      --delay 2.0 \\
      --include-etf no \\
      --confirm YES
"""
from __future__ import annotations

import argparse
import time
from datetime import date, datetime


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"--date-from {s!r} is not YYYY-MM-DD")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Re-fetch tickers the batched backfill dropped.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--exchange", required=True, help="e.g. NSE")
    p.add_argument("--date-from", required=True,
                   help="fetch start (YYYY-MM-DD); same as the backfill's")
    p.add_argument("--min-bars", required=True, type=int,
                   help="re-fetch tickers with fewer than this many 1D bars")
    p.add_argument("--delay", required=True, type=float,
                   help="seconds to sleep between per-ticker fetches")
    p.add_argument("--include-etf", required=True, choices=["yes", "no"],
                   help="also re-fetch ETF instrument_type (many genuinely "
                        "lack deep history)")
    p.add_argument("--config", required=True,
                   help="path to config.yaml (same file the CLI uses)")
    p.add_argument("--confirm", required=True,
                   help="must be the literal string YES to proceed")
    args = p.parse_args()

    if args.confirm != "YES":
        raise SystemExit("Aborted: --confirm must be exactly YES.")
    date_from = _parse_date(args.date_from)

    from sqlalchemy import text
    from trip_ema.config.loader import load_config
    from trip_ema.persistence.connection import create_engine_from_config
    from trip_ema.etl.runner import fetch_bars_for_tickers
    from trip_ema.etl.yfinance_source import YFinanceSource

    config = load_config(args.config)
    db_engine = create_engine_from_config(config)

    # ---- find the stragglers ----
    type_filter = "" if args.include_etf == "yes" else \
        "AND t.instrument_type = 'EQ'"
    with db_engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT t.ticker_id, t.symbol, t.instrument_type,
                   COALESCE(b.n, 0) AS n_bars
            FROM tickers t
            LEFT JOIN (
                SELECT ticker_id, COUNT(*) AS n
                FROM ohlcv_bars WHERE timeframe='1D'
                GROUP BY ticker_id
            ) b ON b.ticker_id = t.ticker_id
            WHERE t.exchange = :ex
              AND t.is_active = TRUE
              {type_filter}
              AND COALESCE(b.n, 0) < :minbars
            ORDER BY t.symbol
        """), {"ex": args.exchange, "minbars": args.min_bars}).all()

    if not rows:
        print("No stragglers found — every active ticker already has "
              f">= {args.min_bars} bars. Nothing to do.")
        return 0

    print(f"Found {len(rows)} stragglers (< {args.min_bars} bars) on "
          f"{args.exchange}. Re-fetching one at a time (delay={args.delay}s)...")

    # solo-fetch source: batch_size=1 avoids the batch-load flakiness
    solo_source = YFinanceSource(
        exchange=args.exchange, batch_size=1,
        request_delay_seconds=0.0,  # we sleep between tickers ourselves
        repair=False,  # D1: IntCastingNaN symbols only recover under repair=False
    )

    recovered = 0
    still_short = []
    for i, r in enumerate(rows, 1):
        try:
            fetch_bars_for_tickers(
                db_engine, config,
                ticker_ids=[r.ticker_id],
                start_date=date_from,
                source=solo_source,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(rows)}] {r.symbol}: fetch error: {e}")
            still_short.append((r.symbol, r.instrument_type, "error"))
            time.sleep(args.delay)
            continue

        # re-check bar count
        with db_engine.connect() as conn:
            n = conn.execute(text("""
                SELECT COUNT(*) FROM ohlcv_bars
                WHERE ticker_id = :tid AND timeframe='1D'
            """), {"tid": r.ticker_id}).scalar() or 0
        if n >= args.min_bars:
            recovered += 1
            print(f"  [{i}/{len(rows)}] {r.symbol}: recovered → {n} bars")
        else:
            still_short.append((r.symbol, r.instrument_type, n))
            print(f"  [{i}/{len(rows)}] {r.symbol}: still short ({n} bars) "
                  f"— likely genuinely dataless")
        time.sleep(args.delay)

    print("\n" + "=" * 50)
    print(f"Recovered: {recovered}/{len(rows)}")
    if still_short:
        print(f"Still short ({len(still_short)}) — genuinely dataless "
              f"(delisted / ETF / too-new / renamed):")
        for sym, itype, n in still_short:
            print(f"  {sym} ({itype}): {n}")
    print("Re-run to retry the still-short set; persistent ones are real gaps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
