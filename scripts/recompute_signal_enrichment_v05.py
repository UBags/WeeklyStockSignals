#!/usr/bin/env python3
"""C5c — recompute signal_enrichment to v0.5 (bands + sequences + validity).

NOT a patcher. An operational script, staged like the other scripts/ tools,
run against the live DB. It changes DATA, so it does nothing destructive
without showing you the numbers first and making you type --confirm YES.

WHAT IT DOES

  1. PRE-FLIGHT (read-only). Counts, by feature_spec_version, how many
     signal_enrichment rows exist; how many belong to tickers that are still
     active NSE; and — the number that matters — how many belong to tickers
     that are NOT active (delisted, deactivated). Prints the recompute scope
     it WOULD use. Stops here unless --confirm YES.

  2. RECOMPUTE. Calls backfill_signal_enrichment with scoped_tickers =
     (DISTINCT ticker_id already in signal_enrichment) UNION (active NSE).
     The union is the whole point: scoping to active tickers alone would
     leave every row on a delisted ticker frozen at v0.4 forever. Those rows
     would then read data_is_valid=false (the 0053 default) and quietly
     survive as garbage in every ML pull. The union revisits them.

  3. VERIFY (read-only). After the run: every row is v0.5; the data_is_valid
     split; a bars_available histogram (so you can SEE how much of the
     history actually supports the atr40 family before you build on it); and
     the day_of_week distribution.

WHAT IT NEVER DOES

  It never DELETEs. The upsert is ON CONFLICT (natural key) DO UPDATE, so it
  updates rows in place and signal_enrichment_id is preserved. signal_labels
  and signal_ensemble_features hang off that id with ON DELETE CASCADE — a
  DELETE here would silently take the labels (your training target y) with
  it. If the pre-flight shows stranded rows you want gone, that is a separate,
  deliberate decision made by a human looking at the label counts, not a side
  effect of a recompute.

USAGE (no defaults on the operational knobs)

  # dry run — pre-flight only, changes nothing:
  python scripts/recompute_signal_enrichment_v05.py --config config.yaml \\
      --exchange NSE

  # the real thing:
  python scripts/recompute_signal_enrichment_v05.py --config config.yaml \\
      --exchange NSE --confirm YES
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python scripts/recompute_signal_enrichment_v05.py` from
# repo root, matching scripts/probe_shares_depth.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


PREFLIGHT_SQL = """
WITH se AS (
    SELECT ticker_id, feature_spec_version
    FROM signal_enrichment
    WHERE timeframe = :tf
),
active AS (
    SELECT ticker_id FROM tickers
    WHERE exchange = :ex AND is_active = TRUE
)
SELECT
    (SELECT count(*) FROM se)                                   AS total_rows,
    (SELECT count(*) FROM se
       WHERE feature_spec_version = :ver)                       AS already_v05,
    (SELECT count(*) FROM se
       WHERE feature_spec_version <> :ver)                      AS stale_rows,
    (SELECT count(DISTINCT ticker_id) FROM se)                  AS enriched_tickers,
    (SELECT count(*) FROM active)                               AS active_tickers,
    (SELECT count(*) FROM se
       WHERE ticker_id NOT IN (SELECT ticker_id FROM active))  AS rows_on_inactive,
    (SELECT count(DISTINCT ticker_id) FROM se
       WHERE ticker_id NOT IN (SELECT ticker_id FROM active))  AS inactive_tickers
"""

SCOPE_SQL = """
SELECT ticker_id FROM signal_enrichment WHERE timeframe = :tf
UNION
SELECT ticker_id FROM tickers WHERE exchange = :ex AND is_active = TRUE
"""

VERIFY_VERSION_SQL = """
SELECT feature_spec_version, count(*) AS rows
FROM signal_enrichment WHERE timeframe = :tf
GROUP BY feature_spec_version ORDER BY feature_spec_version
"""

VERIFY_VALID_SQL = """
SELECT data_is_valid, count(*) AS rows
FROM signal_enrichment WHERE timeframe = :tf
GROUP BY data_is_valid ORDER BY data_is_valid
"""

VERIFY_BARS_SQL = """
SELECT
    CASE
        WHEN bars_available IS NULL      THEN 'NULL (not computed)'
        WHEN bars_available < 15         THEN '00-14  (no atr15+)'
        WHEN bars_available < 20         THEN '15-19  (atr15 only)'
        WHEN bars_available < 30         THEN '20-29  (atr20)'
        WHEN bars_available < 40         THEN '30-39  (atr30)'
        ELSE                                  '40+    (atr40 usable)'
    END                                  AS bars_bucket,
    count(*)                             AS rows
FROM signal_enrichment WHERE timeframe = :tf
GROUP BY bars_bucket ORDER BY bars_bucket
"""

VERIFY_DOW_SQL = """
SELECT day_of_week, count(*) AS rows
FROM signal_enrichment WHERE timeframe = :tf
GROUP BY day_of_week ORDER BY day_of_week
"""

# The invariant the recompute is supposed to establish. After a full run with
# no failed tickers, every row must be v0.5 — anything else is a row the scope
# missed, which is exactly the stranded-inactive bug this script exists to
# avoid. Checked and reported; it does not raise, because a partial run (some
# tickers failed) legitimately leaves stale rows and you want to see that.
POST_STALE_SQL = """
SELECT count(*) AS still_stale
FROM signal_enrichment
WHERE timeframe = :tf AND feature_spec_version <> :ver
"""


def _print_table(title, rows, cols):
    print(f"\n{title}")
    print("  " + "  ".join(f"{c:>22}" for c in cols))
    for r in rows:
        print("  " + "  ".join(f"{str(v):>22}" for v in r))


def main() -> int:
    p = argparse.ArgumentParser(
        description="Recompute signal_enrichment to v0.5.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", required=True,
                   help="path to config.yaml (same file the CLI uses)")
    p.add_argument("--exchange", required=True, help="e.g. NSE")
    p.add_argument("--timeframe", default="1W",
                   help="timeframe to recompute (default 1W)")
    p.add_argument("--confirm", default="",
                   help="must be exactly YES to actually recompute; "
                        "omit for a read-only pre-flight")
    args = p.parse_args()

    from sqlalchemy import text
    from trip_ema.config.loader import load_config
    from trip_ema.persistence.connection import create_engine_from_config
    from trip_ema.fundamentals.signal_enrichment_runner import (
        DEFAULT_SPEC_VERSION, backfill_signal_enrichment,
    )

    config = load_config(args.config)
    db_engine = create_engine_from_config(config)
    bind = {"tf": args.timeframe, "ex": args.exchange,
            "ver": DEFAULT_SPEC_VERSION}

    # ---- 1. PRE-FLIGHT (read-only) -------------------------------------
    with db_engine.connect() as conn:
        pf = conn.execute(text(PREFLIGHT_SQL), bind).one()._mapping

    print("=" * 68)
    print(f"PRE-FLIGHT — signal_enrichment, {args.exchange} {args.timeframe}, "
          f"target {DEFAULT_SPEC_VERSION}")
    print("=" * 68)
    print(f"  rows total                     : {pf['total_rows']:>10,}")
    print(f"    already {DEFAULT_SPEC_VERSION:<6}               : "
          f"{pf['already_v05']:>10,}")
    print(f"    stale (< target)             : {pf['stale_rows']:>10,}")
    print(f"  tickers with enrichment rows   : "
          f"{pf['enriched_tickers']:>10,}")
    print(f"  active {args.exchange} tickers          : "
          f"{pf['active_tickers']:>10,}")
    print(f"  rows on INACTIVE tickers       : "
          f"{pf['rows_on_inactive']:>10,}   <- would strand at v0.4 if scoped "
          f"to active-only")
    print(f"  inactive tickers with rows     : "
          f"{pf['inactive_tickers']:>10,}")

    with db_engine.connect() as conn:
        scope = [r[0] for r in conn.execute(text(SCOPE_SQL), bind)]
    print(f"\n  RECOMPUTE SCOPE (enriched UNION active): {len(scope):,} tickers")
    if pf["rows_on_inactive"]:
        print("  -> the union INCLUDES the inactive-ticker rows above, so no "
              "row is left behind.")

    if args.confirm != "YES":
        print("\nDry run (no --confirm YES). Nothing was changed.")
        print("Re-run with --confirm YES to recompute.")
        return 0

    # ---- 2. RECOMPUTE --------------------------------------------------
    print("\n" + "=" * 68)
    print(f"RECOMPUTING {len(scope):,} tickers ...")
    print("=" * 68)

    # fan_out emits its own N/total + ETA heartbeat; we just relay it.
    def _progress(label, message):
        print(f"  [{label}] {message}", flush=True)

    outcome = backfill_signal_enrichment(
        db_engine, args.exchange,
        scoped_tickers=scope, timeframe=args.timeframe,
        progress_cb=_progress,
    )
    print(f"\n  done={outcome.done}  failed={len(outcome.failed)}")
    if outcome.failed:
        # outcome.failed is [(ticker_id, error_str), ...]
        print("  FAILED tickers (first 20):")
        for tid, err in list(outcome.failed)[:20]:
            print(f"    {tid}: {err}")

    # ---- 3. VERIFY (read-only) -----------------------------------------
    with db_engine.connect() as conn:
        ver_rows = conn.execute(text(VERIFY_VERSION_SQL), bind).all()
        valid_rows = conn.execute(text(VERIFY_VALID_SQL), bind).all()
        bars_rows = conn.execute(text(VERIFY_BARS_SQL), bind).all()
        dow_rows = conn.execute(text(VERIFY_DOW_SQL), bind).all()
        still_stale = conn.execute(text(POST_STALE_SQL), bind).scalar()

    _print_table("spec version distribution:", ver_rows,
                 ["feature_spec_version", "rows"])
    _print_table("data_is_valid split:", valid_rows,
                 ["data_is_valid", "rows"])
    _print_table("bars_available histogram (atr40 needs 40+):", bars_rows,
                 ["bars_bucket", "rows"])
    _print_table("day_of_week (Mon=1..Sun=7):", dow_rows,
                 ["day_of_week", "rows"])

    print("\n" + "=" * 68)
    if still_stale == 0 and not outcome.failed:
        print("OK — every row is now "
              f"{DEFAULT_SPEC_VERSION}. No row was stranded.")
    elif outcome.failed:
        print(f"PARTIAL — {still_stale:,} rows still stale, because "
              f"{len(outcome.failed)} tickers failed. Fix those and re-run; "
              "the script is idempotent (upsert on the natural key).")
    else:
        print(f"WARNING — {still_stale:,} rows still stale even though no "
              "ticker failed. That should not happen with the union scope; "
              "investigate before trusting the table.")
    print("=" * 68)

    print("\nData operation only — no schema change, nothing to commit. "
          "If the bars_available histogram shows enough 40+ rows, the atr40 "
          "family (and lead/lag analytics) has the history to build on.")
    return 0 if (still_stale == 0 or outcome.failed) else 1


if __name__ == "__main__":
    sys.exit(main())
