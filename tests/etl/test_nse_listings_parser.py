"""Tests for the NSE listings parser. No I/O — uses committed fixture
CSVs that capture the actual NSE schema (with trailing spaces in
column names and idiosyncratic date formats)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from trip_ema.etl.nse_listings_parser import (
    parse_equity_csv,
    parse_etf_csv,
)
from trip_ema.etl.types import InstrumentType


FIXTURES = Path(__file__).parent / "fixtures"


class TestEquityCsvParser:

    def test_parses_three_valid_rows(self):
        listings = parse_equity_csv((FIXTURES / "equity_l_sample.csv").read_bytes())
        # 3 valid EQ rows; the warrant (series='W') is skipped
        assert len(listings) == 3
        symbols = sorted(l.symbol for l in listings)
        assert symbols == ["RELIANCE", "TCS", "ZEEMEDIA"]

    def test_all_marked_as_eq(self):
        listings = parse_equity_csv((FIXTURES / "equity_l_sample.csv").read_bytes())
        assert all(l.instrument_type == InstrumentType.EQ for l in listings)

    def test_reliance_full_record(self):
        listings = parse_equity_csv((FIXTURES / "equity_l_sample.csv").read_bytes())
        reliance = next(l for l in listings if l.symbol == "RELIANCE")
        assert reliance.isin == "INE002A01018"
        assert reliance.full_name == "Reliance Industries Limited"
        assert reliance.listing_date == date(1995, 11, 29)
        assert reliance.face_value == 10.0
        assert reliance.lot_size == 1

    def test_skips_non_eq_series(self):
        """The warrant row with series='W' must be excluded — we only
        want EQ and BE series equities."""
        listings = parse_equity_csv((FIXTURES / "equity_l_sample.csv").read_bytes())
        assert not any(l.symbol == "SOMERANDOMWARRANT" for l in listings)

    def test_empty_csv_returns_empty_list(self):
        # Just a header, no data
        empty = b"SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING\n"
        assert parse_equity_csv(empty) == []


class TestEtfCsvParser:

    def test_parses_three_etfs(self):
        listings = parse_etf_csv((FIXTURES / "etf_sample.csv").read_bytes())
        assert len(listings) == 3
        symbols = sorted(l.symbol for l in listings)
        assert symbols == ["BANKBEES", "GOLDBEES", "NIFTYBEES"]

    def test_all_marked_as_etf(self):
        listings = parse_etf_csv((FIXTURES / "etf_sample.csv").read_bytes())
        assert all(l.instrument_type == InstrumentType.ETF for l in listings)

    def test_niftybees_full_record(self):
        listings = parse_etf_csv((FIXTURES / "etf_sample.csv").read_bytes())
        nifty = next(l for l in listings if l.symbol == "NIFTYBEES")
        assert nifty.isin == "INF204KB14I2"
        assert nifty.listing_date == date(2001, 12, 28)
        assert nifty.face_value == 10.0


class TestParserEdgeCases:

    def test_alternative_isin_column_name(self):
        """The ETF CSV historically used 'ISIN Code' or 'ISIN NUMBER'.
        Tolerate both."""
        csv1 = (
            b"Symbol,ISIN Code,Listing Date\n"
            b"TEST1,INE000A01234,01-JAN-2020\n"
        )
        csv2 = (
            b"SYMBOL,ISIN NUMBER,DATE OF LISTING\n"
            b"TEST2,INE000B05678,01-JAN-2020\n"
        )
        l1 = parse_etf_csv(csv1)
        l2 = parse_etf_csv(csv2)
        assert l1[0].isin == "INE000A01234"
        assert l2[0].isin == "INE000B05678"

    def test_dash_isin_treated_as_none(self):
        """NSE sometimes writes '-' for missing ISINs."""
        csv = (
            b"SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,ISIN NUMBER\n"
            b"NOISIN,No ISIN Co,EQ,01-JAN-2020,-\n"
        )
        listings = parse_equity_csv(csv)
        assert listings[0].isin is None

    def test_malformed_date_returns_none(self):
        csv = (
            b"SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,ISIN NUMBER\n"
            b"BADDATE,Bad Date Co,EQ,2024/13/45,INE001A01234\n"
        )
        listings = parse_equity_csv(csv)
        assert listings[0].listing_date is None
        # But the row is still returned — bad date doesn't kill the listing
        assert listings[0].symbol == "BADDATE"

    def test_symbol_uppercased(self):
        """NSE typically uppercases but be defensive."""
        csv = (
            b"SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,ISIN NUMBER\n"
            b"lowersym,Lower Co,EQ,01-JAN-2020,INE001A01234\n"
        )
        listings = parse_equity_csv(csv)
        assert listings[0].symbol == "LOWERSYM"

    def test_headers_with_trailing_whitespace(self):
        """NSE CSVs sometimes have ' DATE OF LISTING' with a leading
        space. The normalizer strips/replaces."""
        csv = (
            b" SYMBOL , NAME OF COMPANY ,SERIES, DATE OF LISTING ,ISIN NUMBER\n"
            b"SPACED,Spaced Co,EQ,01-JAN-2020,INE001A01234\n"
        )
        listings = parse_equity_csv(csv)
        assert listings[0].symbol == "SPACED"
        assert listings[0].isin == "INE001A01234"
