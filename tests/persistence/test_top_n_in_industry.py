"""DB tests for `top_n_tickers_in_industry_by_mcap`.

Pins down the query's contract:
  - Returns only tickers in the requested industry.
  - Orders by latest market cap DESC.
  - Caps the result at N.
  - Latest mcap per ticker means: each ticker's MOST RECENT row in
    daily_market_cap wins, regardless of how many historical rows
    it has.
  - Active filter: is_active=TRUE AND delisted_at IS NULL excludes
    delisted/inactive tickers.
  - Exchange filter scopes by tickers.exchange.
  - Tickers without an instrument_industry row are excluded (the
    JOIN filters them out).

If the previous drop's `market_cap_inr` typo had ever existed in
the live tree, this test would have surfaced it as
'column "market_cap_inr" does not exist' on the first run.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.gui.queries import top_n_tickers_in_industry_by_mcap
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import InstrumentIndustryRepository
from trip_ema.persistence.tickers_repo import TickersRepository


# ---------- helpers ---------- #


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _insert_ticker(conn, symbol: str, *, exchange: str = "NSE",
                   is_active: bool = True, now=None) -> int:
    return TickersRepository(conn).insert(
        TickerRef(exchange, symbol, "EQ"),
        created_at=now or datetime.now(timezone.utc),
        is_active=is_active,
    )


def _classify(conn, ticker_id: int, *, industry: str, sector: str = "X"):
    InstrumentIndustryRepository(conn).upsert(
        ticker_id, sector=sector, industry=industry,
    )


def _insert_mcap(
    conn, ticker_id: int, *,
    trade_date: date,
    market_cap: Decimal | float | int,
    close_price: float = 100.0,
    shares_used: int = 1_000_000,
    source: str = "COMPUTED",
):
    """Insert a row into daily_market_cap. No repo exists — the ETL
    inserts via the runner; we use raw SQL here, which is what the
    real query reads anyway."""
    conn.execute(text("""
        INSERT INTO daily_market_cap
            (ticker_id, trade_date, close_price, shares_used,
             market_cap, source)
        VALUES (:tid, :td, :cp, :su, :mc, :src)
    """), {
        "tid": ticker_id, "td": trade_date,
        "cp": close_price, "su": shares_used,
        "mc": str(Decimal(str(market_cap))),
        "src": source,
    })


# ---------- contract: ordering + cap ---------- #


def test_orders_by_latest_mcap_desc(db_engine, now):
    """Three tickers in one industry; mcap order RELIANCE > HDFC > TCS.
    Query should return them in that order regardless of insert order."""
    today = date.today()
    with transaction(db_engine) as conn:
        tcs   = _insert_ticker(conn, "TCS", now=now)
        rel   = _insert_ticker(conn, "RELIANCE", now=now)
        hdfc  = _insert_ticker(conn, "HDFCBANK", now=now)
        for tid in (tcs, rel, hdfc):
            _classify(conn, tid, industry="Capital Markets")
        _insert_mcap(conn, tcs,  trade_date=today, market_cap=10_00_000)
        _insert_mcap(conn, rel,  trade_date=today, market_cap=99_00_000)
        _insert_mcap(conn, hdfc, trade_date=today, market_cap=50_00_000)

    with db_engine.connect() as conn:
        rows = top_n_tickers_in_industry_by_mcap(
            conn, "Capital Markets", n=10,
        )
    assert [r.symbol for r in rows] == ["RELIANCE", "HDFCBANK", "TCS"]


def test_respects_n_cap(db_engine, now):
    """5 tickers, n=2 → exactly 2 rows back, the top 2 by mcap."""
    today = date.today()
    with transaction(db_engine) as conn:
        for i, sym in enumerate(["A", "B", "C", "D", "E"]):
            tid = _insert_ticker(conn, sym, now=now)
            _classify(conn, tid, industry="TestIndustry")
            # Mcap = (i + 1) * 10k crores so order is E > D > C > B > A
            _insert_mcap(
                conn, tid, trade_date=today,
                market_cap=(i + 1) * 10_000 * 10_000_000,
            )
    with db_engine.connect() as conn:
        rows = top_n_tickers_in_industry_by_mcap(
            conn, "TestIndustry", n=2,
        )
    assert len(rows) == 2
    assert [r.symbol for r in rows] == ["E", "D"]


def test_latest_mcap_per_ticker_wins(db_engine, now):
    """A ticker with multiple historical mcap rows: the query must
    use the LATEST one for ranking, not an older or the smallest."""
    today = date.today()
    with transaction(db_engine) as conn:
        small = _insert_ticker(conn, "SMALL", now=now)
        big   = _insert_ticker(conn, "BIG", now=now)
        for tid in (small, big):
            _classify(conn, tid, industry="Sector")
        # SMALL has an old huge mcap but its most-recent is tiny
        _insert_mcap(conn, small, trade_date=today - timedelta(days=30),
                     market_cap=999_999 * 10_000_000)
        _insert_mcap(conn, small, trade_date=today,
                     market_cap=100 * 10_000_000)
        # BIG has a single moderate mcap, more recent than SMALL's tiny one
        _insert_mcap(conn, big, trade_date=today,
                     market_cap=10_000 * 10_000_000)
    with db_engine.connect() as conn:
        rows = top_n_tickers_in_industry_by_mcap(
            conn, "Sector", n=5,
        )
    # BIG should rank above SMALL because SMALL's LATEST mcap is 100Cr
    # vs BIG's 10kCr. The query must not consider SMALL's old huge row.
    assert [r.symbol for r in rows] == ["BIG", "SMALL"]


# ---------- contract: filtering ---------- #


def test_excludes_other_industries(db_engine, now):
    """A ticker in a different industry must NOT appear in the result."""
    today = date.today()
    with transaction(db_engine) as conn:
        match = _insert_ticker(conn, "MATCH", now=now)
        other = _insert_ticker(conn, "OTHER", now=now)
        _classify(conn, match, industry="TargetIndustry")
        _classify(conn, other, industry="OtherIndustry")
        _insert_mcap(conn, match, trade_date=today, market_cap=100)
        _insert_mcap(conn, other, trade_date=today, market_cap=999_999)
    with db_engine.connect() as conn:
        rows = top_n_tickers_in_industry_by_mcap(
            conn, "TargetIndustry", n=10,
        )
    assert [r.symbol for r in rows] == ["MATCH"]


def test_excludes_delisted_and_inactive(db_engine, now):
    """Even if a ticker has an industry and a latest mcap, it must
    be filtered out when is_active=FALSE or delisted_at is set."""
    today = date.today()
    with transaction(db_engine) as conn:
        active   = _insert_ticker(conn, "ACTIVE", now=now)
        inactive = _insert_ticker(conn, "INACTIVE", now=now, is_active=False)
        _classify(conn, active,   industry="X")
        _classify(conn, inactive, industry="X")
        _insert_mcap(conn, active,   trade_date=today, market_cap=100)
        _insert_mcap(conn, inactive, trade_date=today, market_cap=999_999)
        # Mark a third ticker delisted via raw SQL (no repo write-path).
        delisted = _insert_ticker(conn, "DELISTED", now=now)
        _classify(conn, delisted, industry="X")
        _insert_mcap(conn, delisted, trade_date=today, market_cap=999_999)
        conn.execute(text("""
            UPDATE tickers SET delisted_at = NOW()
            WHERE symbol = 'DELISTED'
        """))
    with db_engine.connect() as conn:
        rows = top_n_tickers_in_industry_by_mcap(conn, "X", n=10)
    assert [r.symbol for r in rows] == ["ACTIVE"]


def test_excludes_tickers_without_industry_classification(db_engine, now):
    """No instrument_industry row → ticker not in result (JOIN filter)."""
    today = date.today()
    with transaction(db_engine) as conn:
        classified = _insert_ticker(conn, "CLASSIFIED", now=now)
        unclassified = _insert_ticker(conn, "ORPHAN", now=now)
        _classify(conn, classified, industry="Sector")
        # ORPHAN has mcap but NO instrument_industry row
        _insert_mcap(conn, classified, trade_date=today, market_cap=100)
        _insert_mcap(conn, unclassified, trade_date=today, market_cap=999_999)
    with db_engine.connect() as conn:
        rows = top_n_tickers_in_industry_by_mcap(conn, "Sector", n=10)
    assert [r.symbol for r in rows] == ["CLASSIFIED"]


def test_exchange_filter(db_engine, now):
    today = date.today()
    with transaction(db_engine) as conn:
        nse = _insert_ticker(conn, "NSEONLY", exchange="NSE", now=now)
        bse = _insert_ticker(conn, "BSEONLY", exchange="BSE", now=now)
        for tid in (nse, bse):
            _classify(conn, tid, industry="Shared")
            _insert_mcap(conn, tid, trade_date=today, market_cap=1000)
    with db_engine.connect() as conn:
        nse_rows = top_n_tickers_in_industry_by_mcap(
            conn, "Shared", n=10, exchange="NSE",
        )
        bse_rows = top_n_tickers_in_industry_by_mcap(
            conn, "Shared", n=10, exchange="BSE",
        )
    assert [r.symbol for r in nse_rows] == ["NSEONLY"]
    assert [r.symbol for r in bse_rows] == ["BSEONLY"]


# ---------- contract: empty / sparse cases ---------- #


def test_unknown_industry_returns_empty(db_engine):
    with db_engine.connect() as conn:
        rows = top_n_tickers_in_industry_by_mcap(
            conn, "NoSuchIndustry", n=12,
        )
    assert rows == []


def test_industry_with_no_mcap_rows_returns_empty(db_engine, now):
    """Ticker has industry but NO daily_market_cap row → excluded
    by the JOIN to latest_mcap."""
    with transaction(db_engine) as conn:
        tid = _insert_ticker(conn, "NOMCAP", now=now)
        _classify(conn, tid, industry="EmptyMcapIndustry")
    with db_engine.connect() as conn:
        rows = top_n_tickers_in_industry_by_mcap(
            conn, "EmptyMcapIndustry", n=12,
        )
    assert rows == []


def test_industry_with_fewer_than_n_tickers(db_engine, now):
    """If only 3 tickers exist in the industry and n=12, we get 3."""
    today = date.today()
    with transaction(db_engine) as conn:
        for sym in ("A", "B", "C"):
            tid = _insert_ticker(conn, sym, now=now)
            _classify(conn, tid, industry="Tiny")
            _insert_mcap(conn, tid, trade_date=today, market_cap=100)
    with db_engine.connect() as conn:
        rows = top_n_tickers_in_industry_by_mcap(conn, "Tiny", n=12)
    assert len(rows) == 3