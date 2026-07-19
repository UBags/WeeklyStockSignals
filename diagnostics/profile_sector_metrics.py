#!/usr/bin/env python3
"""READ-ONLY profiler for stage_6b_sector_metrics, one industry at a time.

Answers the question that decides the optimisation: of the wall time spent on
one industry, how much is DATABASE (loading 20 years of daily bars per
constituent, once per industry the stock belongs to) and how much is COMPUTE
(the 21-lag rolling Spearman in compute_sector_leadlag)?

Both are plausible from reading the code, and they need opposite fixes --
caching versus vectorising -- so measure before building either.

Runs `_metrics_for_industry` unchanged under cProfile, then buckets cumulative
time by what the frames actually belong to. Nothing is written: the repo
upsert that `backfill_sector_metrics._one` normally performs is deliberately
NOT called, so this is safe to run against a live database mid-backfill.

  python profile_sector_metrics.py                     # auto-pick mid-size
  python profile_sector_metrics.py --industry "Banks"
  python profile_sector_metrics.py --list              # sizes, then exit
  python profile_sector_metrics.py --top 30
"""
from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import sys
import time

from sqlalchemy import text

from trip_ema.config import load_config
from trip_ema.domain.market_session import market_session_for
from trip_ema.fundamentals.sector_metrics_runner import _metrics_for_industry
from trip_ema.persistence import transaction
from trip_ema.persistence.connection import create_engine_from_config

SIZES_SQL = """
SELECT i.industry_name,
       count(DISTINCT c.ticker_id) AS constituents
  FROM industry_index i
  LEFT JOIN industry_index_constituent c
         ON c.industry_name = i.industry_name
 WHERE i.status = 'ACTIVE'
 GROUP BY i.industry_name
 ORDER BY constituents DESC
"""

# Frame-name fragments -> bucket. First match wins, so order matters.
BUCKETS = (
    ("DB (driver)", ("psycopg2", "sqlalchemy", "/engine/", "/pool/",
                     "cursor.execute", "do_execute")),
    ("bar load + weekly rollup", ("bars_repo", "fetch_since",
                                  "daily_to_weekly", "aggregation")),
    ("leadlag compute", ("sector_leadlag", "_spearman", "_rankdata",
                         "compute_sector_leadlag")),
    ("numpy", ("numpy", "<__array_function__", "corrcoef", "unique",
               "cov ")),
    ("rs_rank compute", ("sector_cross_section", "compute_rs_rank_by_week")),
)


def _bucket_for(frame: str) -> str | None:
    for name, frags in BUCKETS:
        if any(f in frame for f in frags):
            return name
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exchange", default="NSE")
    ap.add_argument("--industry", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    engine = create_engine_from_config(load_config(args.config))
    session = market_session_for(args.exchange)

    with engine.connect() as conn:
        sizes = conn.execute(text(SIZES_SQL)).all()

    if not sizes:
        print("No ACTIVE industries. Has stage 3c run?")
        return 0

    if args.list:
        print(f"{'industry':44s} constituents")
        for r in sizes:
            print(f"{r.industry_name:44s} {r.constituents:5d}")
        total = sum(r.constituents for r in sizes)
        print(f"\n{len(sizes)} industries, {total} constituent rows, "
              f"median {sizes[len(sizes) // 2].constituents}")
        return 0

    if args.industry:
        industry = args.industry
        n_cons = next((r.constituents for r in sizes
                       if r.industry_name == industry), None)
        if n_cons is None:
            print(f"{industry!r} is not an ACTIVE industry. "
                  f"Try --list.")
            return 1
    else:
        mid = sizes[len(sizes) // 2]
        industry, n_cons = mid.industry_name, mid.constituents

    print(f"Profiling {industry!r} ({n_cons} constituents) "
          f"of {len(sizes)} ACTIVE industries.")
    print("READ ONLY -- the repo upsert is not called.\n")

    prof = cProfile.Profile()
    t0 = time.monotonic()
    with transaction(engine) as conn:
        prof.enable()
        rs_rows, ll_rows = _metrics_for_industry(conn, industry, session)
        prof.disable()
    wall = time.monotonic() - t0

    print(f"wall: {wall:.1f}s   rs_rank rows: {len(rs_rows):,}   "
          f"leadlag rows: {len(ll_rows):,}\n")

    st = pstats.Stats(prof)
    totals: dict[str, float] = {}
    calls: dict[str, int] = {}
    for (fn_file, _lineno, fn_name), (cc, _nc, tt, _ct, _cal) in st.stats.items():
        frame = f"{fn_file}:{fn_name}"
        b = _bucket_for(frame)
        if b is None:
            continue
        # tt is TOTAL time in the frame itself, exclusive of subcalls --
        # summing tt across buckets cannot double-count the way ct would.
        totals[b] = totals.get(b, 0.0) + tt
        calls[b] = calls.get(b, 0) + cc

    accounted = sum(totals.values())
    print("=" * 68)
    print(f"{'bucket':30s} {'self-time':>10s} {'share':>7s} {'calls':>12s}")
    print("-" * 68)
    for b, secs in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"{b:30s} {secs:9.2f}s {secs / wall:6.1%} {calls[b]:12,}")
    print(f"{'(other / unattributed)':30s} "
          f"{wall - accounted:9.2f}s {(wall - accounted) / wall:6.1%}")
    print("=" * 68)

    db = totals.get("DB (driver)", 0.0) + totals.get(
        "bar load + weekly rollup", 0.0)
    cpu = (totals.get("leadlag compute", 0.0) + totals.get("numpy", 0.0)
           + totals.get("rs_rank compute", 0.0))
    print(f"\n  DB-ish   : {db:7.2f}s  ({db / wall:.0%})   -> fix by caching")
    print(f"  CPU-ish  : {cpu:7.2f}s  ({cpu / wall:.0%})   -> fix by vectorising")

    total_cons = sum(r.constituents for r in sizes)
    if n_cons:
        est = wall / n_cons * total_cons
        print(f"\n  Naive extrapolation over all {len(sizes)} industries "
              f"({total_cons} constituent rows):")
        print(f"    ~{est / 60:.0f} min  -- a floor, not a forecast: the "
              f"leadlag step is quadratic-ish in")
        print("    constituent count, so big industries cost far more than "
              "linear scaling implies.")

    print(f"\n--- top {args.top} frames by cumulative time ---")
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(args.top)
    print("\n".join(buf.getvalue().splitlines()[4:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
