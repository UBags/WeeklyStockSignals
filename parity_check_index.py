#!/usr/bin/env python3
"""Parity check — compute_index_values_for_range vs the per-day
compute_index_value_for_date. Run BEFORE the range path is wired into the
value backfill: for an industry it computes every existing trading day
BOTH ways and reports any mismatch in value / constituents_used /
constituents_missing / renormalized / effective_rebalance_date.

Zero mismatches == the batched range path is safe to wire.

    python parity_check_index.py "<industry_name>" [config_path]
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from trip_ema.config.loader import load_config
from trip_ema.persistence.connection import create_engine_from_config
from trip_ema.fundamentals.index_compute import (
    compute_index_value_for_date,
    compute_index_values_for_range,
)


def main(argv) -> int:
    if len(argv) < 2:
        print("usage: parity_check_index.py <industry_name> [config_path]")
        return 2
    industry = argv[1]
    cfg_path = argv[2] if len(argv) > 2 else "config.yaml"
    engine = create_engine_from_config(load_config(cfg_path))

    with engine.connect() as conn:
        dates = [
            r.trade_date for r in conn.execute(
                text("SELECT trade_date FROM industry_index_value "
                     "WHERE industry_name = :n ORDER BY trade_date"),
                {"n": industry},
            )
        ]
    if not dates:
        print(f"No existing values for {industry!r}; nothing to compare.")
        return 1

    range_rows = {
        cv.trade_date: cv
        for cv in compute_index_values_for_range(engine, industry, dates)
    }

    mism = 0
    for d in dates:
        per = compute_index_value_for_date(engine, industry, d)
        rng = range_rows.get(d)
        if (per is None) != (rng is None):
            mism += 1
            print(f"MISMATCH {d}: per_day={per is not None}, "
                  f"range={rng is not None}")
            continue
        if per is None:
            continue
        if (abs(per.value - rng.value) > 1e-9
                or per.constituents_used != rng.constituents_used
                or per.constituents_missing != rng.constituents_missing
                or per.renormalized != rng.renormalized
                or per.effective_rebalance_date
                != rng.effective_rebalance_date):
            mism += 1
            print(f"MISMATCH {d}: per={per.value} range={rng.value} "
                  f"(used {per.constituents_used}/{rng.constituents_used} "
                  f"miss {per.constituents_missing}/"
                  f"{rng.constituents_missing})")

    print(f"\n{industry}: checked {len(dates)} dates, {mism} mismatch(es)")
    print("PARITY OK — safe to wire" if mism == 0
          else "PARITY FAILED — do NOT wire")
    return 0 if mism == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
