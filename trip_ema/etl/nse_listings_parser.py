"""NSE listings parser.

Pure functions that turn CSV bytes into NseListing records. No I/O,
no network — kept separate from the downloader so the parser can be
tested against committed fixture CSVs.

NSE publishes two relevant CSVs:

  EQUITY_L.csv:
    SYMBOL, NAME OF COMPANY,  SERIES, DATE OF LISTING, PAID UP VALUE,
    MARKET LOT, ISIN NUMBER, FACE VALUE

  eq_etfseclist.csv (the ETF list — column names vary; we tolerate
  both observed schemas):
    Symbol, Security Name, Underlying, ISIN Code, Listing Date,
    Face Value, Market Lot
    -- OR --
    SYMBOL, NAME OF COMPANY, ISIN NUMBER, ...

We normalize both into NseListing(symbol, instrument_type, isin,
full_name, listing_date, face_value, lot_size).
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Iterator

from .types import InstrumentType, NseListing


def parse_equity_csv(csv_bytes: bytes) -> list[NseListing]:
    """Parse EQUITY_L.csv into a list of EQ NseListings.

    The CSV has trailing whitespace in some header columns ("SYMBOL "
    vs "SYMBOL"); we normalize. Series 'EQ' or 'BE' rows are returned
    as equities; other series codes are skipped (they're typically
    debt or special instruments we don't care about here).
    """
    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    out: list[NseListing] = []
    for row in reader:
        norm = _normalize_keys(row)
        series = (norm.get("series") or "").strip().upper()
        # EQ = equity; BE = book-entry (also equity, special settlement).
        # We accept both. Series like W (warrants), N (debt) are skipped.
        if series and series not in ("EQ", "BE"):
            continue
        symbol = (norm.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        out.append(NseListing(
            symbol=symbol,
            instrument_type=InstrumentType.EQ,
            isin=_clean_isin(norm.get("isinnumber") or norm.get("isincode")),
            full_name=_strip_or_none(norm.get("nameofcompany")),
            listing_date=_parse_date(norm.get("dateoflisting")),
            face_value=_parse_float(norm.get("facevalue")),
            lot_size=_parse_int(norm.get("marketlot")),
        ))
    return out


def parse_etf_csv(csv_bytes: bytes) -> list[NseListing]:
    """Parse the ETF listing CSV. The column set varies more than
    the equity one — we tolerate either 'Symbol' or 'SYMBOL', either
    'Security Name' or 'NAME OF COMPANY', etc."""
    text = csv_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    out: list[NseListing] = []
    for row in reader:
        norm = _normalize_keys(row)
        symbol = (norm.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        out.append(NseListing(
            symbol=symbol,
            instrument_type=InstrumentType.ETF,
            isin=_clean_isin(norm.get("isincode") or norm.get("isinnumber")),
            full_name=_strip_or_none(
                norm.get("securityname") or norm.get("nameofcompany")
            ),
            listing_date=_parse_date(norm.get("listingdate") or norm.get("dateoflisting")),
            face_value=_parse_float(norm.get("facevalue")),
            lot_size=_parse_int(norm.get("marketlot")),
        ))
    return out


# ---------- helpers ---------- #


def _normalize_keys(row: dict) -> dict:
    """Lowercase + strip whitespace + remove spaces from CSV headers
    so we can look up by canonical key regardless of NSE's formatting
    quirks."""
    return {
        (k or "").strip().lower().replace(" ", "").replace("_", ""): v
        for k, v in row.items()
    }


def _strip_or_none(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip()
    return s or None


def _clean_isin(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip().upper()
    # NSE sometimes has blanks or "-" for missing ISINs
    if not s or s == "-":
        return None
    # ISINs are 12 chars (2 country + 9 alphanumeric + 1 check). Don't
    # validate strictly — accept anything 10-12 chars that's not blank.
    if not (10 <= len(s) <= 12):
        return None
    return s


def _parse_date(v: str | None) -> date | None:
    if not v:
        return None
    s = v.strip()
    if not s:
        return None
    # NSE has used DD-MMM-YYYY (e.g., "20-NOV-2023") and DD-MMM-YY
    # historically; tolerate both plus ISO.
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(v: str | None) -> float | None:
    if not v:
        return None
    s = v.strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(v: str | None) -> int | None:
    f = _parse_float(v)
    if f is None:
        return None
    return int(f)
