"""NSE shareholding-pattern XBRL spike (Phase 2 reconnaissance).

THIS IS A THROWAWAY SPIKE — not production code. The goal is to
answer concrete questions about NSE's shareholding-pattern data
source before committing to a production design:

  Q1. Can we successfully bootstrap an NSE session and reach the
      JSON API at all? (NSE has anti-bot hardening on these endpoints.)
  Q2. Does the JSON response shape match our assumptions?
  Q3. Does each company's latest filing actually have an XBRL link?
  Q4. Are the XBRL files parseable with lxml (no XBRL-specific
      library needed)?
  Q5. Does the concept name `NumberOfFullyPaidUpEquityShares` actually
      appear in the files, or do different filings use different
      concept URIs / taxonomy versions?
  Q6. Do the parsed numbers match the public truth for these 5 names?
      (Operator hand-checks against annual reports / NSE announcements.)

Run:
    python3 scripts/spike_nse_xbrl.py 2>&1 | tee /tmp/nse_xbrl_spike.log

It prints a structured table to stdout and writes the raw XBRL files
(one per ticker) to /tmp/spike_xbrl_<symbol>.xml for inspection.

If the script fails at a particular step, the next-action note in the
output tells the operator (or me, on resume) what to look at.

Sample: 5 NSE tickers covering large/mid/small caps and different
promoter structures.

DELIBERATE NON-PRODUCTION SHORTCUTS:
  - No retry/backoff. One shot per request. If NSE blocks us, we
    learn it now, not after a 4-hour run.
  - No DB writes. Pure stdout.
  - No caching. Every run hits NSE fresh — slower but reveals
    whether the cookie chain is reliable across runs.
  - No yfinance fallback path. The whole point is to prove THIS source.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("ERROR: `pip install requests lxml` first", file=sys.stderr)
    sys.exit(2)
try:
    from lxml import etree
except ImportError:
    print("ERROR: `pip install requests lxml` first", file=sys.stderr)
    sys.exit(2)


# Mix of large/mid/small + promoter-structure variation.
# Names chosen to maximize taxonomy variation in the spike.
SAMPLES = [
    ("RELIANCE",   "large",  "family-promoter"),
    ("INFY",       "large",  "professional-mgmt"),
    ("PERSISTENT", "mid",    "professional-mgmt"),
    ("BHARATFORG", "mid",    "family-promoter"),
    ("KSCL",       "small",  "family-promoter"),
]

# Where NSE's anti-bot wants us to come from.
HOMEPAGE_URL = "https://www.nseindia.com"

# The shareholding-pattern API endpoint discovered by inspecting NSE's
# browser network calls. This is the BEST GUESS — the spike will tell
# us if it's right. The two-parameter shape (`index=equities&symbol=X`)
# is what other NSE-related Python libraries (jugaad-data, nselib) use
# for similar endpoints; the shareholding endpoint may have its own
# parameter shape.
SHP_API_URL = (
    "https://www.nseindia.com/api/corporate-share-holdings-master"
    "?index=equities&symbol={symbol}"
)

# Browser-like headers. NSE blocks naked Python requests immediately;
# we need to look like Firefox. The Accept header is significant —
# NSE serves different content based on what we say we'll accept.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) "
        "Gecko/20100101 Firefox/127.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern",
    "Connection": "keep-alive",
}


@dataclass
class SpikeResult:
    symbol: str
    cap_tier: str
    promoter_structure: str
    # Each phase records its outcome separately so we can see WHICH
    # step broke if anything did.
    session_ok: Optional[bool] = None
    api_status: Optional[int] = None
    api_payload_shape: Optional[str] = None   # short description
    latest_filing_date: Optional[str] = None
    xbrl_url: Optional[str] = None
    xbrl_status: Optional[int] = None
    xbrl_bytes: Optional[int] = None
    concept_uri_found: Optional[str] = None   # which concept URI matched
    shares_outstanding: Optional[int] = None
    error: Optional[str] = None
    notes: list[str] = None

    def __post_init__(self):
        if self.notes is None:
            self.notes = []


def bootstrap_session() -> Optional[requests.Session]:
    """Hit NSE's homepage to acquire the anti-bot cookies. Without this,
    every subsequent API call returns 401 or 403."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    try:
        r = s.get(HOMEPAGE_URL, timeout=10)
        # NSE's homepage redirects internally; any 2xx outcome means
        # we got cookies. The exact cookie names vary; we don't try
        # to assert anything specific.
        if r.status_code != 200:
            print(f"  WARN: homepage returned {r.status_code}", file=sys.stderr)
            return None
        if not s.cookies:
            print(f"  WARN: no cookies set after homepage", file=sys.stderr)
            return None
        # Some operators report needing a second hit for the cookies
        # to "settle" — try a market-status call which is cheaper than
        # the homepage and reliably sets the post-bootstrap cookies.
        try:
            s.get("https://www.nseindia.com/api/marketStatus", timeout=10)
        except Exception:
            pass
        return s
    except Exception as e:
        print(f"  ERROR bootstrapping session: {e}", file=sys.stderr)
        return None


def fetch_shareholding_index(s: requests.Session, symbol: str) -> tuple[int, dict | str]:
    """Hit the SHP JSON API for one symbol. Returns (status_code, body).
    Body is a dict if JSON parsed, else a short error string with the
    first 200 chars of the response."""
    url = SHP_API_URL.format(symbol=quote(symbol))
    try:
        r = s.get(url, timeout=15)
        if r.status_code != 200:
            return r.status_code, f"HTTP {r.status_code}: {r.text[:200]}"
        try:
            return 200, r.json()
        except json.JSONDecodeError:
            return 200, f"non-JSON body: {r.text[:300]}"
    except Exception as e:
        return -1, f"exception: {type(e).__name__}: {e}"


def find_latest_xbrl_url(payload: dict) -> tuple[Optional[str], Optional[str], str]:
    """Walk the JSON payload looking for the latest filing's XBRL link.
    Returns (filing_date, xbrl_url, shape_description). The shape
    description is a short string for the report so we can see if
    NSE's payload structure is what we expected."""
    if not isinstance(payload, dict):
        return None, None, f"not a dict: {type(payload).__name__}"

    # Probe the top-level keys for context. NSE's various endpoints have
    # used different envelope shapes ("data", "Records", "results", etc.).
    top_keys = list(payload.keys())[:8]
    shape = f"top-level keys: {top_keys}"

    # Common shapes to try in order.
    candidates = (
        payload.get("data"),
        payload.get("Records"),
        payload.get("rows"),
        payload.get("shareholdingsPattern"),
    )
    rows = next((c for c in candidates if isinstance(c, list)), None)
    if rows is None and isinstance(payload, dict):
        # Maybe the dict itself is keyed by some other category. Look
        # for any list child that has dict elements (a filings list).
        for k, v in payload.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                rows = v
                shape += f"; using list under {k!r}"
                break

    if not rows:
        return None, None, shape + " (no list of filings found)"

    # Find the row with the most recent filing date that has an XBRL URL.
    # The date field name is one of {"date", "as_on_date", "submissionDate",
    # "filingDate"}; we look for any of them and pick the max.
    def row_date(r: dict) -> str:
        for k in ("date", "as_on_date", "submissionDate", "filingDate", "asOnDate"):
            v = r.get(k)
            if v:
                return str(v)
        return ""

    def row_xbrl(r: dict) -> Optional[str]:
        for k in ("xbrl", "xbrlLink", "xbrlUrl", "xbrl_url", "fileXbrl"):
            v = r.get(k)
            if v and isinstance(v, str) and v.startswith("http"):
                return v
        return None

    with_xbrl = [(row_date(r), row_xbrl(r), r) for r in rows]
    with_xbrl = [(d, u, r) for d, u, r in with_xbrl if u]
    if not with_xbrl:
        sample = rows[0] if rows else {}
        return None, None, (
            shape + f"; found {len(rows)} filings, none had an xbrl url "
            f"(sample row keys: {list(sample.keys())[:10]})"
        )
    with_xbrl.sort(key=lambda t: t[0], reverse=True)
    date, url, _ = with_xbrl[0]
    return date, url, shape + f"; {len(with_xbrl)} filings have xbrl, latest {date}"


def fetch_xbrl_file(s: requests.Session, url: str) -> tuple[int, bytes | str]:
    try:
        r = s.get(url, timeout=20)
        if r.status_code != 200:
            return r.status_code, f"HTTP {r.status_code}: {r.text[:200]}"
        return 200, r.content
    except Exception as e:
        return -1, f"exception: {type(e).__name__}: {e}"


# Possible concept URIs across SEBI taxonomy versions. The spike tries
# all of them; whichever matches tells us which taxonomy this filing
# uses. In production we'd union these into one resilient query.
CONCEPT_CANDIDATES = (
    "NumberOfFullyPaidUpEquityShares",
    "NumberOfFullyPaidupEquityShares",
    "NoOfFullyPaidUpEquityShares",
    "NumberOfEquityShares",
)


def parse_shares_outstanding(xbrl_bytes: bytes) -> tuple[Optional[int], Optional[str], list[str]]:
    """Find the shares-outstanding number in the XBRL. Returns
    (shares, matching_concept_uri, notes). Notes captures useful
    observations about the file's structure even if parsing failed."""
    notes: list[str] = []
    try:
        root = etree.fromstring(xbrl_bytes)
    except etree.XMLSyntaxError as e:
        return None, None, [f"XML parse error: {e}"]

    # Capture the namespaces — that tells us which taxonomy.
    nsmap = root.nsmap or {}
    notes.append(f"namespaces: {dict(list(nsmap.items())[:6])}")
    notes.append(f"root tag: {root.tag}")

    # For each candidate concept, try a localname match across all
    # elements. We use localname rather than full namespace because
    # different taxonomies use different namespace URIs for the same
    # logical concept.
    for concept in CONCEPT_CANDIDATES:
        # XPath with local-name() to ignore namespace.
        xpath = f".//*[local-name()='{concept}']"
        matches = root.xpath(xpath)
        if not matches:
            continue

        # An XBRL file has multiple <Concept> elements, one per
        # context (different shareholder categories: total, public,
        # promoter, etc.). We want the TOTAL — which usually has
        # context "OneOfReportingClassesIPublicShareholding" or
        # similar. For the spike we just collect ALL values and
        # the operator looks at them.
        values = []
        for el in matches:
            t = (el.text or "").strip()
            ctx = el.get("contextRef") or el.get("{http://www.xbrl.org/2003/instance}contextRef")
            if t:
                try:
                    values.append((ctx, int(t.replace(",", ""))))
                except ValueError:
                    pass

        if not values:
            notes.append(f"  concept {concept}: matched {len(matches)} elements but no parseable integers")
            continue

        notes.append(f"  concept {concept}: {len(values)} value(s) found")
        for ctx, v in values[:5]:
            notes.append(f"    {ctx} = {v:,}")

        # Naive: the largest value is usually the total (sum of all
        # shareholder categories ≥ any single category). Production
        # code should be context-aware.
        shares = max(v for _, v in values)
        return shares, concept, notes

    notes.append("NO matching concept URI found — taxonomy is unfamiliar")
    return None, None, notes


def main() -> int:
    print("=" * 70)
    print("NSE SHAREHOLDING-PATTERN XBRL SPIKE")
    print("=" * 70)
    print()

    # ---- Phase 0: bootstrap session ---- #
    print("Bootstrapping NSE session ...")
    s = bootstrap_session()
    if s is None:
        print("\n[FATAL] Could not bootstrap session. Cannot proceed.")
        print("  Next action: check that this machine has network access to")
        print("  www.nseindia.com and isn't behind a corporate proxy.")
        return 2
    print(f"  cookies acquired: {len(s.cookies)} items")
    print()

    # ---- Phase 1: per-ticker probe ---- #
    results = []
    for symbol, tier, structure in SAMPLES:
        print(f"--- {symbol} ({tier}, {structure}) ---")
        r = SpikeResult(symbol=symbol, cap_tier=tier, promoter_structure=structure)
        r.session_ok = True

        # 1a. Hit the SHP API.
        status, body = fetch_shareholding_index(s, symbol)
        r.api_status = status
        if status != 200 or not isinstance(body, dict):
            r.error = f"SHP API failed: {body if isinstance(body, str) else body}"
            print(f"  [FAIL] {r.error[:200]}")
            results.append(r); time.sleep(2); continue

        # 1b. Find the latest filing's XBRL URL.
        date, xbrl_url, shape = find_latest_xbrl_url(body)
        r.api_payload_shape = shape
        r.latest_filing_date = date
        r.xbrl_url = xbrl_url
        print(f"  api shape: {shape}")
        if not xbrl_url:
            r.error = "No XBRL URL found in payload"
            print(f"  [FAIL] {r.error}")
            results.append(r); time.sleep(2); continue
        print(f"  latest filing: {date}")
        print(f"  xbrl url: {xbrl_url}")

        # 1c. Download the XBRL.
        xstatus, xbody = fetch_xbrl_file(s, xbrl_url)
        r.xbrl_status = xstatus
        if xstatus != 200 or not isinstance(xbody, bytes):
            r.error = f"XBRL fetch failed: {xbody if isinstance(xbody, str) else 'no body'}"
            print(f"  [FAIL] {r.error[:200]}")
            results.append(r); time.sleep(2); continue
        r.xbrl_bytes = len(xbody)
        print(f"  xbrl downloaded: {r.xbrl_bytes:,} bytes")

        # Save the file so the operator can eyeball it.
        outpath = Path(f"/tmp/spike_xbrl_{symbol}.xml")
        outpath.write_bytes(xbody)
        print(f"  saved to: {outpath}")

        # 1d. Parse for shares outstanding.
        shares, concept, parse_notes = parse_shares_outstanding(xbody)
        r.shares_outstanding = shares
        r.concept_uri_found = concept
        r.notes.extend(parse_notes)
        for n in parse_notes:
            print(f"  {n}")
        if shares is None:
            r.error = "Could not extract shares-outstanding from XBRL"
            print(f"  [PARTIAL] file fetched but parse failed")
        else:
            print(f"  [OK] shares_outstanding = {shares:,} via concept {concept!r}")

        results.append(r)
        time.sleep(2)  # be polite to NSE

    # ---- Phase 2: summary table ---- #
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(
        f"{'SYMBOL':<12} {'TIER':<6} {'STRUCTURE':<22} "
        f"{'API':<5} {'XBRL':<5} {'CONCEPT':<35} {'SHARES':>20}"
    )
    for r in results:
        api = str(r.api_status or "-")
        xbrl = str(r.xbrl_status or "-")
        concept = (r.concept_uri_found or "-")[:33]
        shares = f"{r.shares_outstanding:,}" if r.shares_outstanding else "-"
        print(
            f"{r.symbol:<12} {r.cap_tier:<6} {r.promoter_structure:<22} "
            f"{api:<5} {xbrl:<5} {concept:<35} {shares:>20}"
        )

    # ---- Phase 3: machine-readable JSON ---- #
    print()
    print("=" * 70)
    print("JSON OUTPUT (for the design conversation)")
    print("=" * 70)
    print(json.dumps([
        {
            "symbol": r.symbol,
            "tier": r.cap_tier,
            "structure": r.promoter_structure,
            "api_status": r.api_status,
            "api_payload_shape": r.api_payload_shape,
            "latest_filing_date": r.latest_filing_date,
            "xbrl_url": r.xbrl_url,
            "xbrl_status": r.xbrl_status,
            "xbrl_bytes": r.xbrl_bytes,
            "concept_uri_found": r.concept_uri_found,
            "shares_outstanding": r.shares_outstanding,
            "error": r.error,
            "notes": r.notes,
        }
        for r in results
    ], indent=2))

    # Exit code: 0 if at least 3 of 5 worked end-to-end; 1 if some
    # worked; 2 if nothing worked.
    full_success = sum(1 for r in results if r.shares_outstanding is not None)
    print()
    print(f"VERDICT: {full_success}/{len(results)} tickers parsed end-to-end")
    if full_success == 0:
        return 2
    if full_success < len(results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
