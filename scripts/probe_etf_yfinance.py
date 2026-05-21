"""Recon script: probe yfinance for Indian ETF metadata.

Run this on a machine with network access:
    python3 scripts/probe_etf_yfinance.py

It samples a dozen Indian ETFs (mixing sectoral, broad-market,
commodity, debt, and international) and prints whatever metadata
yfinance returns. The output drives the schema design for the
instrument_etf table — we don't want to commit to columns that
yfinance never populates for Indian ETFs.

Output is JSON to stdout; tee it to a file and paste back to the
design conversation.
"""
from __future__ import annotations

import json
import sys
import time

import yfinance as yf


# A deliberately diverse sample — sectoral, broad-market, factor,
# commodity, debt, international. If a field is populated for some
# but not others, that's important to know before we commit to
# a schema.
SAMPLES = [
    # Sector ETFs
    ("ABSLBANETF.NS", "ABSL Bank ETF — sectoral (banking)"),
    ("AUTOBEES.NS",   "Nippon Auto BeES — sectoral (auto)"),
    ("ITBEES.NS",     "Nippon IT BeES — sectoral (IT)"),
    ("PHARMABEES.NS", "Nippon Pharma BeES — sectoral (pharma)"),

    # Broad market index
    ("ABSLNN50ET.NS", "ABSL Nifty Next 50 — broad market"),
    ("NIFTYBEES.NS",  "Nippon Nifty BeES — broad market"),
    ("ICICINIFTY.NS", "ICICI Nifty 50 — broad market"),

    # Commodity
    ("GOLDBEES.NS",   "Nippon Gold BeES — commodity"),
    ("SILVERBEES.NS", "Nippon Silver BeES — commodity"),

    # Debt / liquid
    ("ABSLLIQUID.NS", "ABSL Liquid ETF — debt/liquid"),
    ("LIQUIDETF.NS",  "DSP Liquid ETF — debt/liquid"),

    # International
    ("MAFANG.NS",     "Mirae FANG+ ETF — international"),

    # Smart-beta / factor
    ("ALPHA.NS",      "Nippon Alpha ETF — factor"),
]


# Fields we'd plausibly use for ETF classification. We dump ALL of
# them so the operator can see exactly what's populated and what's
# NULL across the sample.
FIELDS_TO_SHOW = [
    # Identity
    "symbol", "shortName", "longName", "quoteType",

    # ETF-specific classification candidates
    "category",            # Yahoo's category label
    "categoryName",        # alternate field name in some versions
    "fundFamily",          # AMC / fund house
    "fundInceptionDate",
    "longBusinessSummary", # often describes what the ETF tracks

    # Underlying / holdings hints (sometimes populated)
    "sector",              # rare but seen for some sector ETFs
    "industry",            # ditto

    # Financial bits (may not be classification, but useful context)
    "totalAssets",
    "navPrice",
    "yield",
    "ytdReturn",
    "expenseRatio",        # if yfinance has it
]


def probe(symbol: str, description: str) -> dict:
    """Fetch one ticker and return a dict of {field: value} for the
    fields we care about. Errors caught and returned as {"_error": ...}."""
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}
    out = {"_description": description}
    for f in FIELDS_TO_SHOW:
        v = info.get(f)
        if v is not None:
            # Truncate long strings so the output is readable.
            if isinstance(v, str) and len(v) > 200:
                v = v[:200] + "..."
            out[f] = v
    # Also note which fields we DIDN'T find — that's the important
    # signal for schema design.
    missing = [f for f in FIELDS_TO_SHOW if f not in out and f != "_description"]
    if missing:
        out["_missing"] = missing
    return out


def main() -> int:
    print("=" * 70, file=sys.stderr)
    print(f"Probing {len(SAMPLES)} Indian ETFs from yfinance.", file=sys.stderr)
    print(f"This will take ~{len(SAMPLES) * 2}s with the 2s/ticker delay.", file=sys.stderr)
    print("=" * 70, file=sys.stderr)

    results = {}
    for sym, desc in SAMPLES:
        print(f"  fetching {sym} ({desc}) ...", file=sys.stderr)
        results[sym] = probe(sym, desc)
        time.sleep(2.0)  # be polite

    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
