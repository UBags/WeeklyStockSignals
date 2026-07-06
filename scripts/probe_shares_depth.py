"""Step 0 — shares-depth probe for the 18-20y industry-history extension.

THROWAWAY DIAGNOSTIC (not production code). One job: measure the
achievable history floor for each industry index BEFORE we commit to
deepening daily_market_cap / building a rebalance chain / rebuilding IIV.

THE GATE
--------
industry_index_value can only be computed from base_date onward, and a
day is only computable for a constituent when we have BOTH:
  - a local 1d price bar (ohlcv_bars), and
  - a shares-outstanding value (yfinance get_shares_full), because the
    production fetcher (fetch_historical_mcap_for_ticker) DROPS any price
    date earlier than the first get_shares_full row.
So per ticker:  data_first_date = max(price_first_date, shares_first_date).
Per industry:   floor = the data_first_date of the min_constituents-th
                earliest computable candidate. That is the earliest date
                the index can field a full constituent set.

Price depth is read from the DB (free). Only shares depth needs yfinance,
so we probe a decision-relevant candidate set (current top-K by market cap
per indexed industry) rather than the whole 2667-ticker universe.

CURRENT-SIZE PROXY (know this before trusting the number): candidates are
ranked by CURRENT market cap. A firm that was large 15y ago but is small
now can be missed. That only makes the reported floor CONSERVATIVE (the
true achievable depth is >= what we report), which is the safe direction
for a go/no-go gate. Use --scope universe to remove the proxy at the cost
of a much longer run.

RUN (on the NUC, inside the venv, from the repo root):
    python scripts/probe_shares_depth.py --config config.yaml \
        --scope candidates --candidate-multiple 3 \
        --target-date 2008-04-01 \
        --out /tmp/shares_depth.csv 2>&1 | tee /tmp/shares_depth.log

Resumable: per-ticker rows are written to --out as they complete; re-running
skips symbols already present. Safe to Ctrl-C and restart.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

# Allow running as `python scripts/probe_shares_depth.py` from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import text  # noqa: E402

from trip_ema.config.loader import load_config  # noqa: E402
from trip_ema.persistence.connection import create_engine_from_config  # noqa: E402

# Minimal, self-contained yfinance suffix map (probe scope: NSE/BSE + US).
_SUFFIX = {"NSE": ".NS", "BSE": ".BO", "NYSE": "", "NASDAQ": ""}

CSV_FIELDS = [
    "industry", "exchange", "symbol", "ticker_id",
    "price_first_date", "shares_first_date", "shares_last_date",
    "shares_rows", "data_first_date", "error",
]


# --------------------------------------------------------------------------- #
# DB side (free — no yfinance)
# --------------------------------------------------------------------------- #
def load_index_meta(engine) -> dict[str, dict]:
    """{industry_name: {min_constituents, base_date}} for every live index."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT industry_name, min_constituents, base_date "
            "FROM industry_index"
        )).fetchall()
    return {
        r[0]: {"min_constituents": int(r[1]), "base_date": r[2]}
        for r in rows
    }


def load_candidates(engine, *, k: int | None) -> list[dict]:
    """Top-k current-mcap tickers per INDEXED industry (k=None → all).

    Ranking date = latest trade_date present in daily_market_cap.
    Uses effective industry (override beats instrument_industry), active EQ.
    """
    sql = text("""
        WITH latest AS (SELECT MAX(trade_date) AS d FROM daily_market_cap),
        effective_industry AS (
            SELECT t.ticker_id, t.symbol, t.exchange,
                   COALESCE(io.industry, ii.industry) AS industry
            FROM tickers t
            LEFT JOIN industry_override io ON io.ticker_id = t.ticker_id
            LEFT JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
            WHERE t.is_active = TRUE AND t.instrument_type = 'EQ'
        ),
        ranked AS (
            SELECT ei.industry, ei.ticker_id, ei.symbol, ei.exchange,
                   mc.market_cap,
                   ROW_NUMBER() OVER (
                       PARTITION BY ei.industry ORDER BY mc.market_cap DESC
                   ) AS rn
            FROM effective_industry ei
            JOIN daily_market_cap mc ON mc.ticker_id = ei.ticker_id
            JOIN latest ON mc.trade_date = latest.d
            WHERE ei.industry IS NOT NULL AND mc.market_cap > 0
        )
        SELECT industry, ticker_id, symbol, exchange, market_cap
        FROM ranked
        WHERE industry IN (SELECT industry_name FROM industry_index)
          AND (:k IS NULL OR rn <= :k)
        ORDER BY industry, rn
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"k": k}).fetchall()
    return [
        {"industry": r[0], "ticker_id": int(r[1]), "symbol": r[2],
         "exchange": r[3], "market_cap": float(r[4])}
        for r in rows
    ]


def load_price_first_dates(engine, ticker_ids: list[int]) -> dict[int, date]:
    """MIN(1d bar) per ticker — the local price-history floor. One query."""
    if not ticker_ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ticker_id, MIN(bar_timestamp) AS first_ts
            FROM ohlcv_bars
            WHERE timeframe = '1d' AND ticker_id = ANY(:ids)
            GROUP BY ticker_id
        """), {"ids": ticker_ids}).fetchall()
    out: dict[int, date] = {}
    for tid, ts in rows:
        out[int(tid)] = ts.date() if hasattr(ts, "date") else ts
    return out


# --------------------------------------------------------------------------- #
# yfinance side (the expensive part)
# --------------------------------------------------------------------------- #
def yf_symbol(symbol: str, exchange: str) -> str:
    if exchange not in _SUFFIX:
        raise ValueError(f"unknown exchange {exchange!r} for {symbol!r}")
    return f"{symbol.upper()}{_SUFFIX[exchange]}"


def fetch_shares_depth(symbol: str, exchange: str, *, retries: int = 3):
    """Return (first_date, last_date, n_rows). Retries on transient errors.

    Mirrors the production dependency exactly: yfinance Ticker.get_shares_full.
    """
    import yfinance as yf

    yfs = yf_symbol(symbol, exchange)
    last_err = None
    for attempt in range(retries):
        try:
            t = yf.Ticker(yfs)
            getter = getattr(t, "get_shares_full", None)
            if getter is None:
                raise RuntimeError("get_shares_full not available (yfinance too old)")
            s = getter()
            if s is None or len(s) == 0:
                return None, None, 0
            dates = []
            for ts, v in zip(s.index, s.values):
                if v is None or (isinstance(v, float) and v != v):  # NaN
                    continue
                d = ts.date() if hasattr(ts, "date") else ts
                dates.append(d)
            if not dates:
                return None, None, 0
            dates.sort()
            return dates[0], dates[-1], len(dates)
        except Exception as e:  # noqa: BLE001 — probe: catch, back off, retry
            last_err = e
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))  # crude backoff for 429s
    raise last_err  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# CSV (incremental + resume)
# --------------------------------------------------------------------------- #
def load_done(out_path: Path) -> dict[tuple[str, str], dict]:
    if not out_path.exists():
        return {}
    done: dict[tuple[str, str], dict] = {}
    with out_path.open(newline="") as f:
        for row in csv.DictReader(f):
            done[(row["exchange"], row["symbol"])] = row
    return done


def append_row(out_path: Path, row: dict) -> None:
    new = not out_path.exists()
    with out_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def _parse_d(s):
    if not s:
        return None
    if isinstance(s, date):
        return s
    return datetime.strptime(s, "%Y-%m-%d").date()


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def summarize(rows: list[dict], index_meta: dict[str, dict], target: date) -> None:
    by_ind: dict[str, list[date]] = defaultdict(list)
    for r in rows:
        dfd = _parse_d(r.get("data_first_date"))
        if dfd is not None:
            by_ind[r["industry"]].append(dfd)

    print("\n" + "=" * 78)
    print(f"PER-INDUSTRY FLOOR  (target = {target.isoformat()}, "
          f"floor = min_constituents-th earliest computable candidate)")
    print("=" * 78)
    print(f"{'industry':<34}{'min':>4}{'cand':>6}{'floor':>13}  reaches?")
    print("-" * 78)

    reach = block = insufficient = 0
    year_hist: dict[int, int] = defaultdict(int)
    for industry in sorted(index_meta):
        mc = index_meta[industry]["min_constituents"]
        dates = sorted(by_ind.get(industry, []))
        n = len(dates)
        if n < mc:
            floor_s, verdict = "—", "INSUFFICIENT"
            insufficient += 1
        else:
            floor = dates[mc - 1]  # the min_constituents-th earliest
            floor_s = floor.isoformat()
            year_hist[floor.year] += 1
            if floor <= target:
                verdict = "YES"
                reach += 1
            else:
                verdict = f"no (from {floor.year})"
                block += 1
        print(f"{industry[:34]:<34}{mc:>4}{n:>6}{floor_s:>13}  {verdict}")

    total = len(index_meta)
    print("-" * 78)
    print(f"VERDICT: {reach}/{total} indices reach {target.isoformat()};  "
          f"{block} shallower;  {insufficient} lack enough computable candidates.")
    if year_hist:
        print("\nFloor-year histogram (indices with a computable floor):")
        for yr in sorted(year_hist):
            print(f"  {yr}: {'#' * year_hist[yr]} ({year_hist[yr]})")
    print("\nPer-ticker detail is in the --out CSV. A ticker's floor is gated by "
          "max(price_first_date, shares_first_date); compare the two columns to "
          "see whether shares or local bars is the binding constraint.")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Step 0 shares-depth probe.")
    ap.add_argument("--config", required=True)
    ap.add_argument("--scope", choices=["candidates", "universe"],
                    default="candidates")
    ap.add_argument("--candidate-multiple", type=int, default=3,
                    help="top-K per industry = multiple × min_constituents "
                         "(ignored for --scope universe)")
    ap.add_argument("--target-date", default="2008-04-01",
                    help="history floor we want to reach (a semiannual anchor)")
    ap.add_argument("--out", default="/tmp/shares_depth.csv")
    ap.add_argument("--pace-seconds", type=float, default=1.5)
    args = ap.parse_args()

    target = datetime.strptime(args.target_date, "%Y-%m-%d").date()
    out_path = Path(args.out)

    cfg = load_config(args.config)
    engine = create_engine_from_config(cfg)
    try:
        index_meta = load_index_meta(engine)
        if not index_meta:
            print("No rows in industry_index — nothing to probe.", file=sys.stderr)
            return 1

        if args.scope == "universe":
            k = None
        else:
            # widen so shallow-shares tickers don't starve the min_const count
            max_min = max(m["min_constituents"] for m in index_meta.values())
            k = args.candidate_multiple * max_min

        candidates = load_candidates(engine, k=k)
        price_first = load_price_first_dates(
            engine, [c["ticker_id"] for c in candidates]
        )

        print(f"Indices: {len(index_meta)} | candidates to probe: "
              f"{len(candidates)} | scope={args.scope} | k={k}", file=sys.stderr)

        done = load_done(out_path)
        rows: list[dict] = list(done.values())

        for i, c in enumerate(candidates, 1):
            key = (c["exchange"], c["symbol"])
            if key in done:
                continue
            pfd = price_first.get(c["ticker_id"])
            try:
                sfd, sld, srows = fetch_shares_depth(c["symbol"], c["exchange"])
                err = ""
            except Exception as e:  # noqa: BLE001
                sfd = sld = None
                srows = 0
                err = f"{type(e).__name__}: {e}"[:120]

            # data_first_date requires BOTH price and shares.
            dfd = None
            if pfd is not None and sfd is not None:
                dfd = max(pfd, sfd)

            row = {
                "industry": c["industry"], "exchange": c["exchange"],
                "symbol": c["symbol"], "ticker_id": c["ticker_id"],
                "price_first_date": pfd.isoformat() if pfd else "",
                "shares_first_date": sfd.isoformat() if sfd else "",
                "shares_last_date": sld.isoformat() if sld else "",
                "shares_rows": srows,
                "data_first_date": dfd.isoformat() if dfd else "",
                "error": err,
            }
            append_row(out_path, row)
            rows.append(row)

            if i % 25 == 0 or err:
                tag = f"  ERR {err}" if err else ""
                print(f"  [{i}/{len(candidates)}] {c['symbol']}.{c['exchange']} "
                      f"shares_first={row['shares_first_date'] or 'NONE'}{tag}",
                      file=sys.stderr)
            time.sleep(args.pace_seconds)

        summarize(rows, index_meta, target)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
