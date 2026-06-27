"""Integration tests for `trip_ema.notifications.compose_email`.

Synthetic fixture seeds:
  - 2-4 tickers in `tickers` + `instrument_industry`
  - Optional stock_benchmark_stats + industry_benchmark_stats for
    rank-score testing
  - Rows in `current_signals` directly (bypassing the cycle)

Then calls `compose_email(conn, subscriber, cycle_id, now)` and
asserts on the returned EmailBody (or None).
"""
from __future__ import annotations

from datetime import date, datetime, UTC

import pytest
from sqlalchemy import text

from trip_ema.notifications.compose_email import (
    EmailBody,
    compose_email,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.subscribers_repo import SubscriberRecord


# ============================================================
# Helpers
# ============================================================

_SUBSCRIBER = SubscriberRecord(
    subscriber_id=1,
    transport="email",
    address="test@example.com",
    notify_cadence_minutes=60,
    last_notified_at=None,
    is_active=True,
)


def _seed_ticker(
    conn, *,
    ticker_id: int, symbol: str, exchange: str = "NSE",
) -> int:
    conn.execute(text("""
        INSERT INTO tickers (ticker_id, exchange, symbol)
        VALUES (:id, :ex, :sym)
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"id": ticker_id, "ex": exchange, "sym": symbol})
    return ticker_id


def _seed_industry_mapping(
    conn, ticker_id: int, *,
    industry: str, sector: str = "Technology",
) -> None:
    """The instrument_industry table has a CHECK constraint
    `classification_source IN ('YFINANCE', 'GROWW',
    'MANUAL_OVERRIDE', 'NSE_SECTORAL', 'INSTRUMENT_TYPE')`.
    MANUAL_OVERRIDE is the appropriate value for synthetic test
    data."""
    conn.execute(text("""
        INSERT INTO instrument_industry (
            ticker_id, sector, industry, classification_source
        ) VALUES (:tid, :sec, :ind, 'MANUAL_OVERRIDE')
        ON CONFLICT (ticker_id) DO UPDATE
           SET industry = EXCLUDED.industry,
               sector = EXCLUDED.sector
    """), {"tid": ticker_id, "sec": sector, "ind": industry})


def _seed_current_signal(
    conn, *,
    ticker_id: int,
    signal_type: str = "BUY",
    regime_before: int = 0,
    regime_after: int = 1,
    price_at_signal: float = 100.0,
) -> None:
    conn.execute(text("""
        INSERT INTO current_signals (
            ticker_id, timeframe, bar_timestamp, signal_type,
            regime_before, regime_after, price_at_signal,
            computed_at
        ) VALUES (
            :tid, '1W',
            TIMESTAMP WITH TIME ZONE '2025-10-06 00:00:00+00',
            :st, :rb, :ra, :p,
            TIMESTAMP WITH TIME ZONE '2025-10-09 14:30:00+00'
        )
        ON CONFLICT (ticker_id) DO UPDATE
           SET signal_type = EXCLUDED.signal_type,
               regime_before = EXCLUDED.regime_before,
               regime_after  = EXCLUDED.regime_after,
               price_at_signal = EXCLUDED.price_at_signal
    """), {
        "tid": ticker_id, "st": signal_type,
        "rb": regime_before, "ra": regime_after,
        "p": price_at_signal,
    })


def _seed_stock_benchmark_stats(
    conn, *,
    ticker_id: int, rs_pct: float, r_squared: float,
    as_of_date: date = date(2025, 10, 6),
) -> None:
    """Seed one row used by fetch_rank_scores. benchmark_kind=INDUSTRY,
    window_label=SHORT matches the rank-score SQL filter."""
    conn.execute(text("""
        INSERT INTO stock_benchmark_stats (
            ticker_id, benchmark_kind, benchmark_id, window_label,
            as_of_date, n_weeks, beta, alpha_annual,
            r_squared, sigma_annual, rs_pct, info_ratio
        ) VALUES (
            :tid, 'INDUSTRY', 'IND_TEST', 'SHORT',
            :ad, 26, 1.0, 0.0,
            :r2, 0.2, :rs, 0.5
        )
        ON CONFLICT (ticker_id, benchmark_kind,
                     window_label, as_of_date) DO UPDATE
           SET rs_pct = EXCLUDED.rs_pct,
               r_squared = EXCLUDED.r_squared
    """), {
        "tid": ticker_id, "ad": as_of_date,
        "rs": rs_pct, "r2": r_squared,
    })


def _seed_industry(conn, *, name: str) -> None:
    """Insert one industry_index row. Required before seeding
    industry_benchmark_stats — that table has an FK on
    industry_index(industry_name). Same canonical pattern used in
    tests/fundamentals/test_v1_runners_industry_indicator_state.py."""
    conn.execute(text("""
        INSERT INTO industry_index (
            industry_name, sector_name, methodology,
            base_value, base_date, rebalance_schedule,
            min_constituents, status
        ) VALUES (
            :name, 'Technology', 'MODIFIED_LASPEYRES',
            1000.0, '2025-01-01', 'SEMIANNUAL_APR_OCT',
            10, 'ACTIVE'
        )
        ON CONFLICT (industry_name) DO NOTHING
    """), {"name": name})


def _seed_industry_benchmark_stats(
    conn, *,
    industry: str, rs_pct: float,
    as_of_date: date = date(2025, 10, 6),
) -> None:
    conn.execute(text("""
        INSERT INTO industry_benchmark_stats (
            industry_name, benchmark_id, window_label, as_of_date,
            n_weeks, beta, alpha_annual,
            r_squared, sigma_annual, rs_pct, info_ratio
        ) VALUES (
            :ind, 'NIFTY500', 'SHORT', :ad,
            26, 1.0, 0.0,
            0.5, 0.2, :rs, 0.5
        )
        ON CONFLICT (industry_name, benchmark_id, window_label,
                     as_of_date) DO UPDATE
           SET rs_pct = EXCLUDED.rs_pct
    """), {"ind": industry, "ad": as_of_date, "rs": rs_pct})


# ============================================================
# Empty case
# ============================================================

class TestEmpty:

    def test_empty_current_signals_returns_none(self, db_engine):
        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="20260626-1500",
                now=datetime.now(UTC),
            )
        assert body is None


# ============================================================
# Basic composition
# ============================================================

class TestBasic:

    def test_single_signal_returns_body(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=9001, symbol="RELIANCE")
            _seed_industry_mapping(
                conn, 9001, industry="Oil & Gas",
            )
            _seed_current_signal(conn, ticker_id=9001)

        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="20260626-1500",
                now=datetime(2026, 6, 26, 15, 0, tzinfo=UTC),
            )

        assert body is not None
        assert isinstance(body, EmailBody)
        assert body.signal_count == 1
        assert "RELIANCE" in body.html
        assert "RELIANCE" in body.plain_text

    def test_signal_count_matches_rows(self, db_engine):
        with transaction(db_engine) as conn:
            for i, sym in enumerate(["A", "B", "C", "D"], start=1):
                tid = 9100 + i
                _seed_ticker(conn, ticker_id=tid, symbol=sym)
                _seed_industry_mapping(
                    conn, tid, industry="Specialty Chemicals",
                )
                _seed_current_signal(conn, ticker_id=tid)

        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="X", now=datetime.now(UTC),
            )
        assert body is not None
        assert body.signal_count == 4

    def test_subject_includes_cycle_id_and_counts(self, db_engine):
        """Subject format:
        '[trip_ema] {cycle_id} — {N} signals (B BUY, S SELL, E EXIT)'.
        """
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=9201, symbol="BUY1")
            _seed_industry_mapping(conn, 9201, industry="X")
            _seed_current_signal(
                conn, ticker_id=9201, signal_type="BUY",
                regime_before=0, regime_after=1,
            )
            _seed_ticker(conn, ticker_id=9202, symbol="SELL1")
            _seed_industry_mapping(conn, 9202, industry="X")
            _seed_current_signal(
                conn, ticker_id=9202, signal_type="SELL",
                regime_before=0, regime_after=-1,
            )
            _seed_ticker(conn, ticker_id=9203, symbol="EXITB")
            _seed_industry_mapping(conn, 9203, industry="X")
            _seed_current_signal(
                conn, ticker_id=9203, signal_type="EXIT_FROM_BUY",
                regime_before=1, regime_after=0,
            )
            _seed_ticker(conn, ticker_id=9204, symbol="EXITS")
            _seed_industry_mapping(conn, 9204, industry="X")
            _seed_current_signal(
                conn, ticker_id=9204, signal_type="EXIT_FROM_SELL",
                regime_before=-1, regime_after=0,
            )

        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="ABC-123", now=datetime.now(UTC),
            )

        assert body is not None
        # 4 total: 1 BUY, 1 SELL, 2 EXIT (combined)
        assert "ABC-123" in body.subject
        assert "4 signals" in body.subject
        assert "1 BUY" in body.subject
        assert "1 SELL" in body.subject
        assert "2 EXIT" in body.subject

    def test_singular_signal_uses_no_s(self, db_engine):
        """'1 signal' not '1 signals'."""
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=9301, symbol="SOLO")
            _seed_industry_mapping(conn, 9301, industry="X")
            _seed_current_signal(conn, ticker_id=9301)

        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="X", now=datetime.now(UTC),
            )
        assert body is not None
        assert "1 signal " in body.subject  # space-delimited token
        assert "1 signals" not in body.subject


# ============================================================
# Grouping by industry
# ============================================================

class TestIndustryGrouping:

    def test_two_industries_each_has_section(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=9401, symbol="OIL1")
            _seed_industry_mapping(conn, 9401, industry="Oil & Gas")
            _seed_current_signal(conn, ticker_id=9401)

            _seed_ticker(conn, ticker_id=9402, symbol="CHEM1")
            _seed_industry_mapping(
                conn, 9402, industry="Specialty Chemicals",
            )
            _seed_current_signal(conn, ticker_id=9402)

        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="X", now=datetime.now(UTC),
            )

        assert body is not None
        # Both industries surface in body
        assert "Oil &amp; Gas" in body.html   # HTML-escaped
        assert "Specialty Chemicals" in body.html
        assert "Oil & Gas" in body.plain_text   # NOT escaped in plain
        assert "Specialty Chemicals" in body.plain_text

    def test_null_industry_groups_under_unknown(self, db_engine):
        """A ticker with no instrument_industry row → groups under
        '(Unknown)'."""
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=9501, symbol="NOIND")
            # Note: no _seed_industry_mapping call
            _seed_current_signal(conn, ticker_id=9501)

        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="X", now=datetime.now(UTC),
            )
        assert body is not None
        assert "(Unknown)" in body.html
        assert "(Unknown)" in body.plain_text


# ============================================================
# Rank-score sorting
# ============================================================

class TestRankSorting:

    def test_higher_rank_appears_first(self, db_engine):
        """Two tickers in the same industry: the one with the higher
        rank_score appears first in both HTML and plain text."""
        with transaction(db_engine) as conn:
            # LOW_RANK: rs_pct=10, r_squared=0.1
            #   → score ≈ 0.5*10 + 0.3*ind_rs + 0.2*0.1
            _seed_ticker(conn, ticker_id=9601, symbol="LOWRANK")
            _seed_industry_mapping(
                conn, 9601, industry="TestInd",
            )
            _seed_current_signal(conn, ticker_id=9601)
            _seed_stock_benchmark_stats(
                conn, ticker_id=9601,
                rs_pct=10.0, r_squared=0.1,
            )

            # HIGH_RANK: rs_pct=80, r_squared=0.7
            _seed_ticker(conn, ticker_id=9602, symbol="HIGHRANK")
            _seed_industry_mapping(
                conn, 9602, industry="TestInd",
            )
            _seed_current_signal(conn, ticker_id=9602)
            _seed_stock_benchmark_stats(
                conn, ticker_id=9602,
                rs_pct=80.0, r_squared=0.7,
            )

            _seed_industry(conn, name="TestInd")
            _seed_industry_benchmark_stats(
                conn, industry="TestInd", rs_pct=50.0,
            )

        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="X", now=datetime.now(UTC),
            )

        assert body is not None
        # HIGHRANK should appear BEFORE LOWRANK in the plain text
        pt = body.plain_text
        hi = pt.find("HIGHRANK")
        lo = pt.find("LOWRANK")
        assert hi != -1 and lo != -1
        assert hi < lo, (
            f"Expected HIGHRANK ({hi}) before LOWRANK ({lo}) in "
            f"plain text — rank sort broken"
        )

    def test_missing_rank_score_treated_as_zero(self, db_engine):
        """Ticker with NO benchmark_stats row: rank should be 0.0
        (not None, not error)."""
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=9701, symbol="NORANK")
            _seed_industry_mapping(conn, 9701, industry="TestInd")
            _seed_current_signal(conn, ticker_id=9701)
            # No _seed_stock_benchmark_stats call

        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="X", now=datetime.now(UTC),
            )

        assert body is not None
        # rank=0.00 should appear in the plain text for this ticker
        assert "rank=0.00" in body.plain_text


# ============================================================
# HTML / plain-text well-formedness
# ============================================================

class TestRenderWellFormed:

    def _setup(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=9801, symbol="WELL1")
            _seed_industry_mapping(conn, 9801, industry="TestInd")
            _seed_current_signal(conn, ticker_id=9801)

    def test_html_has_table(self, db_engine):
        self._setup(db_engine)
        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="X", now=datetime.now(UTC),
            )
        assert body is not None
        assert "<table" in body.html
        assert "</table>" in body.html
        # Standard column headers
        assert "Signal" in body.html
        assert "Ticker" in body.html
        assert "Price" in body.html
        assert "Rank" in body.html

    def test_plain_text_has_no_html_tags(self, db_engine):
        self._setup(db_engine)
        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="X", now=datetime.now(UTC),
            )
        assert body is not None
        # Crude but effective: no '<' (HTML tag start) characters
        # except in URLs (none in our V1 body)
        assert "<" not in body.plain_text
        assert ">" not in body.plain_text

    def test_html_escapes_special_chars_in_industry(self, db_engine):
        """An '&' in an industry name must be HTML-escaped in
        the HTML body."""
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=9901, symbol="AMP")
            _seed_industry_mapping(conn, 9901, industry="A & B")
            _seed_current_signal(conn, ticker_id=9901)

        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="X", now=datetime.now(UTC),
            )
        assert body is not None
        assert "A &amp; B" in body.html
        # Plain text keeps the raw character (no escaping)
        assert "A & B" in body.plain_text


# ============================================================
# Footer / cycle_id propagation
# ============================================================

class TestFooter:

    def test_cycle_id_appears_in_body(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_ticker(conn, ticker_id=10001, symbol="FOOT")
            _seed_industry_mapping(conn, 10001, industry="X")
            _seed_current_signal(conn, ticker_id=10001)

        with db_engine.connect() as conn:
            body = compose_email(
                conn, _SUBSCRIBER,
                cycle_id="UNIQUE-CYCLE-ID-42",
                now=datetime.now(UTC),
            )
        assert body is not None
        assert "UNIQUE-CYCLE-ID-42" in body.html
        assert "UNIQUE-CYCLE-ID-42" in body.plain_text
