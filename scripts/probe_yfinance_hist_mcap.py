"""Probe yfinance for historical market cap availability (Phase 4A).

THIS IS A THROWAWAY SPIKE — not production code. Goal: answer
concrete questions about yfinance's historical mcap API before
committing to a fetcher design.

Questions:
  Q1. Does yfinance expose a historical market cap series for
      Indian tickers, and at what granularity?
  Q2. What's the API surface — Ticker.history(), a custom property,
      or yfinance.download with extras?
  Q3. How far back does the series go for representative names?
  Q4. Does the series differ for large-caps vs mid-caps vs
      small-caps?
  Q5. For dates within our local instrument_shares data range,
      does yfinance's mcap roughly match our close × shares
      computation?

Run:
    python3 scripts/probe_yfinance_hist_mcap.py 2>&1 | tee /tmp/hist_mcap_probe.log

It samples 5 NSE tickers across cap tiers and prints structured
output. Paste back the log so we can design Phase 4A's backfill
strategy against real data shape.

DELIBERATE NON-PRODUCTION SHORTCUTS:
  - No retries. One shot per request.
  - No caching. Every run hits yfinance fresh.
  - No DB writes. Pure stdout.
  - 2-second pacing between tickers (the same pacing the
    production fetchers use, to be polite).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone


# Mix of cap tiers — large/mid/small. Reuses some names from
# spike_nse_xbrl.py so the operator has continuity.
SAMPLES = [
    ("RELIANCE",   "large"),
    ("TCS",        "large"),
    ("PERSISTENT", "mid"),
    ("BHARATFORG", "mid"),
    ("KSCL",       "small"),
]


def probe(symbol: str, tier: str) -> dict:
    """For each sample, attempt several known yfinance approaches
    to surfacing historical mcap. Record what worked and what
    didn't."""
    import yfinance as yf

    yf_symbol = f"{symbol}.NS"
    result: dict = {
        "symbol": symbol,
        "tier": tier,
        "yf_symbol": yf_symbol,
        "approaches": {},
    }
    t = yf.Ticker(yf_symbol)

    # ---- Approach 1: Ticker.history with auto-adjusted close ----
    # yfinance returns OHLCV but NOT mcap directly. We'd have to
    # multiply close × shares ourselves. Record whether history
    # returns a usable DataFrame.
    try:
        hist = t.history(period="1y", actions=False)
        result["approaches"]["history_1y"] = {
            "ok": True,
            "rows": len(hist),
            "first_date": str(hist.index[0].date()) if len(hist) else None,
            "last_date": str(hist.index[-1].date()) if len(hist) else None,
            "columns": list(hist.columns),
        }
    except Exception as e:
        result["approaches"]["history_1y"] = {
            "ok": False, "error": f"{type(e).__name__}: {e}",
        }

    # ---- Approach 2: Ticker.info['marketCap'] ----
    # This is the CURRENT mcap. Not a series. We already use this
    # path elsewhere; recording it for context.
    try:
        info = t.info or {}
        result["approaches"]["info_current_marketcap"] = {
            "ok": True,
            "marketCap": info.get("marketCap"),
            "sharesOutstanding": info.get("sharesOutstanding"),
        }
    except Exception as e:
        result["approaches"]["info_current_marketcap"] = {
            "ok": False, "error": f"{type(e).__name__}: {e}",
        }

    # ---- Approach 3: Ticker.get_shares_full() ----
    # yfinance recently added a method that returns a historical
    # shares-outstanding series. If this works, we can compute
    # historical mcap from close × shares ourselves — much cleaner
    # than relying on a pre-baked yfinance mcap.
    try:
        getter = getattr(t, "get_shares_full", None)
        if getter is None:
            result["approaches"]["get_shares_full"] = {
                "ok": False, "error": "method does not exist on this yfinance version",
            }
        else:
            shares_series = getter()
            if shares_series is None or len(shares_series) == 0:
                result["approaches"]["get_shares_full"] = {
                    "ok": False, "error": "returned empty / None",
                }
            else:
                # Convert index to date strings for JSON; sample first
                # and last few entries.
                idx = [str(x.date() if hasattr(x, "date") else x)
                       for x in shares_series.index]
                vals = [
                    (None if v is None or (isinstance(v, float) and v != v) else int(v))
                    for v in shares_series.values
                ]
                result["approaches"]["get_shares_full"] = {
                    "ok": True,
                    "rows": len(shares_series),
                    "first_date": idx[0],
                    "last_date": idx[-1],
                    "first_value": vals[0],
                    "last_value": vals[-1],
                    "sample_mid": vals[len(vals) // 2] if vals else None,
                }
    except Exception as e:
        result["approaches"]["get_shares_full"] = {
            "ok": False, "error": f"{type(e).__name__}: {e}",
        }

    # ---- Approach 4: Manual computation ----
    # close × current shares for a recent date in the history.
    # Compares against current marketCap from info to validate the
    # math. (We use current shares — yfinance has no historical
    # shares series unless approach 3 worked, in which case we'd
    # also be able to compute period-accurate mcap.)
    try:
        info = t.info or {}
        current_shares = info.get("sharesOutstanding")
        current_mcap = info.get("marketCap")
        hist_30d = t.history(period="1mo", actions=False)
        if (
            current_shares
            and current_mcap
            and len(hist_30d) > 0
        ):
            recent_close = float(hist_30d["Close"].iloc[-1])
            computed_mcap = recent_close * current_shares
            ratio = computed_mcap / current_mcap if current_mcap else None
            result["approaches"]["manual_compute_sanity"] = {
                "ok": True,
                "recent_close": recent_close,
                "current_shares": current_shares,
                "computed_mcap": computed_mcap,
                "current_mcap_from_info": current_mcap,
                "ratio_computed_to_info": ratio,
            }
        else:
            result["approaches"]["manual_compute_sanity"] = {
                "ok": False,
                "error": "missing inputs",
                "current_shares": current_shares,
                "current_mcap": current_mcap,
                "history_rows": len(hist_30d) if hist_30d is not None else 0,
            }
    except Exception as e:
        result["approaches"]["manual_compute_sanity"] = {
            "ok": False, "error": f"{type(e).__name__}: {e}",
        }

    return result


def main() -> int:
    print("=" * 70, file=sys.stderr)
    print(
        f"Probing {len(SAMPLES)} NSE tickers for yfinance historical mcap "
        f"API shape.",
        file=sys.stderr,
    )
    print(
        f"This will take ~{len(SAMPLES) * 5}s with the 2s/ticker pause "
        f"and the 2-3 API calls per ticker.",
        file=sys.stderr,
    )
    print("=" * 70, file=sys.stderr)

    results = []
    for symbol, tier in SAMPLES:
        print(f"  probing {symbol} ({tier})...", file=sys.stderr)
        try:
            results.append(probe(symbol, tier))
        except Exception as e:
            results.append({
                "symbol": symbol, "tier": tier,
                "_outer_error": f"{type(e).__name__}: {e}",
            })
        time.sleep(2.0)

    # ---- Phase 1: structured JSON ----
    print(json.dumps(results, indent=2, default=str))

    # ---- Phase 2: a short readable summary ----
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for r in results:
        sym = r.get("symbol", "?")
        tier = r.get("tier", "?")
        if "_outer_error" in r:
            print(f"  {sym:<12} {tier:<6} OUTER FAIL: {r['_outer_error']}")
            continue
        gsf = r["approaches"].get("get_shares_full", {})
        gsf_ok = gsf.get("ok")
        if gsf_ok:
            print(
                f"  {sym:<12} {tier:<6} get_shares_full: OK "
                f"({gsf.get('rows')} rows, "
                f"{gsf.get('first_date')} → {gsf.get('last_date')})"
            )
        else:
            print(
                f"  {sym:<12} {tier:<6} get_shares_full: NO "
                f"({gsf.get('error', '?')[:60]})"
            )

    # Verdict shapes how Phase 4A's fetcher gets designed:
    #   - If get_shares_full works for all 5 → use it as the
    #     primary historical shares source; close × shares per
    #     date is the cleanest path.
    #   - If it works for some → mixed strategy (use it where it
    #     works, fall back to APPROX_TODAYS_SHARES otherwise).
    #   - If it works for none → we'd need a different yfinance
    #     approach OR accept APPROX_TODAYS_SHARES as the only
    #     option for backfill.
    return 0


if __name__ == "__main__":
    sys.exit(main())
